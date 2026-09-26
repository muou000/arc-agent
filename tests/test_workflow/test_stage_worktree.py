"""Stage worktree lifecycle and serialized publication merge contracts."""

from __future__ import annotations

import subprocess
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

from agents.runtime.capabilities import stable_node_path_segment
from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient
from core import sessions
from core.queue_state import (
    STAGE_BLOCKED,
    STAGE_FAILED,
    STAGE_INTERFACE_DESIGN,
    STAGE_PUBLISHED,
    STAGE_READY_TO_MERGE,
    STAGE_RUNNING,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    TASK_FAILED,
    fail_stage_task,
    load_or_create_queue,
    reset_node_for_retry,
    task_status,
    transition_stage_task,
)
from core.stage_merge_queue import StageMergeQueue
from core.stage_worktree import (
    StagePublication,
    StagePublicationError,
    StageWorktreeManager,
)
from core.worktree import MergeConflictError
from core.workflow import ARCWorkflowManager
from tests.helpers.faux import FauxChatModel, faux_tool_call
from tests.helpers.jsonl import read_jsonl
from tests.test_agents.conftest import arc_runtime  # noqa: F401


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {args} failed: {completed.stderr}")
    return completed


def _init_repo(tmp_path: Path) -> tuple[Path, StageWorktreeManager]:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    (repo / ".gitignore").write_text(
        "# >>> arcbench-agent-runtime >>>\n"
        ".arc/*\n"
        "!.arc/traceability/\n"
        "!.arc/traceability/**\n"
        "# <<< arcbench-agent-runtime >>>\n",
        encoding="utf-8",
    )
    (repo / "backend").mkdir()
    (repo / "backend" / "app.js").write_text("v1;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo, StageWorktreeManager(str(repo))


def _publish(manager: StageWorktreeManager, handle: object, *, writes: list[str]) -> object:
    return manager.publish(
        handle,
        "stage publication",
        declared_write_set=writes,
        contract_hash="contract-hash",
        test_manifest_hash="manifest-hash",
        validation_evidence={"status": "passed"},
    )


def test_prepare_stage_records_integration_base_and_uses_stage_namespace(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    handle = manager.prepare_stage(
        "REQ-1",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/feature.js"],
    )

    assert handle.base_commit == base
    assert handle.branch == "arc-stage/REQ-1/INTERFACE_DESIGN"
    assert Path(handle.path).parent.name == "stage-worktrees"
    assert manager._is_registered(Path(handle.path))


def test_failed_test_stage_retry_starts_without_previous_uncommitted_files(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage("REQ-1", STAGE_TEST_GENERATION)
    root = Path(handle.path)
    test_path = root / "backend" / "tests" / "generated" / "REQ-1" / "auth.test.js"
    helper_path = test_path.with_name("helper.js")
    test_path.parent.mkdir(parents=True)
    test_path.write_text("previous attempt\n", encoding="utf-8")
    helper_path.write_text("old helper\n", encoding="utf-8")
    tracked = root / "backend" / "app.js"
    tracked.write_text("failed edit\n", encoding="utf-8")
    _git(["add", "backend/app.js"], root)

    assert manager.prepare_stage("REQ-1", STAGE_TEST_GENERATION).path == handle.path
    assert test_path.read_text(encoding="utf-8") == "previous attempt\n"

    for attempt in range(2):
        restarted = manager.prepare_stage("REQ-1", STAGE_TEST_GENERATION, restart_failed_attempt=True)
        assert restarted.path == handle.path
        assert not test_path.exists()
        assert not helper_path.exists()
        assert tracked.read_text(encoding="utf-8") == "v1;\n"
        assert _git(["status", "--porcelain"], root).stdout == ""
        if attempt == 0:
            test_path.parent.mkdir(parents=True)
            test_path.write_text("second failed attempt\n", encoding="utf-8")

    assert (repo / "backend" / "app.js").read_text(encoding="utf-8") == "v1;\n"


def test_test_generation_two_failed_attempts_then_resume_registers_written_tests(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The faux model must rewrite after a failed stage; a leftover on disk
    cannot turn the next pass into an unwritten, zero-row manifest.
    """
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo = tmp_project_dir
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    (repo / ".gitignore").write_text(".arc/\n", encoding="utf-8")
    (repo / "backend").mkdir()
    (repo / "backend" / "app.js").write_text("v1;\n", encoding="utf-8")
    _git(["add", ".gitignore", "backend/app.js"], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    node_id = "REQ-1"
    path = f"backend/tests/generated/{stable_node_path_segment(node_id)}/auth.test.js"
    test_code = "test('auth', () => { expect(login()).toBe(true); });\n"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Auth", "description": "Login", "children": []}
    )
    interface = {"interface_id": "REQ-1-FUNC-login", "type": "FUNC", "file_path": "backend/app.js"}
    arc_runtime.traceability.upsert_interface(
        interface_id=interface["interface_id"], req_ids=[node_id], type="FUNC",
        content="{}", file_path="backend/app.js",
    )
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"), {"id": node_id, "children": []}
    )
    interface_task = next(task for task in queue["stage_tasks"] if task["stage"] == STAGE_INTERFACE_DESIGN)
    test_task = next(task for task in queue["stage_tasks"] if task["stage"] == STAGE_TEST_GENERATION)
    interface_task["status"] = STAGE_PUBLISHED
    manager = ARCWorkflowManager(
        workspace_path=str(repo), requirement_path="", app_type="web", web_port=4400,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = arc_runtime
    manager._save_processing_queue = lambda _queue: None
    real_build = manager._build_task_phase_runner
    produced: list[list[dict[str, object]] | None] = []
    runner_saw_existing: list[bool] = []

    def build_runner(workspace: str, port: int | None, handle=None):
        runner = real_build(workspace, port, handle)
        existing = (Path(workspace) / path).exists()
        runner_saw_existing.append(existing)
        responses = [faux_tool_call("declare_stage_write_set", {"paths": [path]}, call_id="s1")]
        if existing:
            # This is the toxic resume: the model reads the old test and
            # declares that it will preserve it, without a new write receipt.
            responses.append(
                faux_tool_call("read_file", {"file_path": f"/workspace/{path}"}, call_id="s-read")
            )
        responses.append(faux_tool_call("declare_test_manifest", {"files": [
            {"file_path": path, "type": "Unit", "interface_ids": [interface["interface_id"]]}
        ]}, call_id="s2"))
        if not existing:
            responses.append(faux_tool_call(
                "write_file", {"file_path": f"/workspace/{path}", "content": test_code}, call_id="s3"
            ))
        responses.append(faux_tool_call("TestGenerationResponse", {
                "summary": "Auth behavior", "tests": [{
                    "test_id": "REQ-1-T-AUTH", "req_id": node_id,
                    "interface_ids": [interface["interface_id"]], "type": "Unit",
                    "file_path": path, "first_line": test_code.strip(),
                }], "files_written": [] if existing else [path],
            }, call_id="s4"))
        model = FauxChatModel(responses=responses)
        runner.test_generator.model = model
        original_run = runner.test_generator.run

        async def record_generation(*args, **kwargs):
            rows, text = await original_run(*args, **kwargs)
            produced.append(rows)
            return rows, text

        runner.test_generator.run = record_generation

        async def baseline(**_kwargs):
            if len(produced) <= 2:
                return None  # Failure after the faux model wrote its test.
            return {"file_state": {path: "red"}, "revised_tests": None}

        runner._enforce_design_baseline_red = baseline
        return runner

    manager._build_task_phase_runner = build_runner
    for attempt in range(3):
        sessions.merge_node_session(node_id, {
            "phase_status": {"design": "prepared"}, "interfaces": [interface],
            "materialized_files": [],
        })
        transition_stage_task(queue, node_id, STAGE_TEST_GENERATION, STAGE_RUNNING)
        result = asyncio.run(manager._execute_stage_task(test_task, queue))
        assert produced[-1] is not None and len(produced[-1]) == 1
        assert runner_saw_existing[-1] is False
        assert test_task["attempt_count"] == attempt + 1
        assert (repo / path).exists() is False  # Stage writes never reach integration before publication.
        stage_file = Path(manager._stage_worktree_manager.worktrees_root) / "REQ-1--TEST_GENERATION" / path
        assert stage_file.read_text(encoding="utf-8") == test_code
        if attempt < 2:
            assert result["status"] == STAGE_FAILED
            assert arc_runtime.traceability.list_tests(req_id=node_id) == []
            transition_stage_task(queue, node_id, STAGE_TEST_GENERATION, STAGE_FAILED)
            reset_node_for_retry(queue, node_id)
            transition_stage_task(queue, node_id, STAGE_INTERFACE_DESIGN, STAGE_PUBLISHED)
        else:
            assert result["status"] == STAGE_READY_TO_MERGE
            rows = arc_runtime.traceability.list_tests(req_id=node_id)
            assert len(rows) == 1
            assert rows[0]["file_path"] == path
            publication = StagePublication.from_dict(result["publication"])
            context = result["_stage_workspace"]
            manager._stage_worktree_manager.integrate_stage(context.handle, publication, "merge tests")
            assert (repo / path).read_text(encoding="utf-8") == test_code
            assert rows[0]["first_line"] == test_code.strip()


def test_publish_requires_complete_metadata_and_declared_write_set(tmp_path: Path) -> None:
    _repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage(
        "REQ-1",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/feature.js"],
    )
    (Path(handle.path) / "backend" / "feature.js").write_text("feature;\n", encoding="utf-8")

    publication = _publish(manager, handle, writes=["backend/feature.js"])

    assert publication.base_commit == handle.base_commit
    assert publication.artifact_commit != publication.base_commit
    assert publication.declared_write_set == ("backend/feature.js",)
    assert publication.contract_hash == "contract-hash"
    assert publication.test_manifest_hash == "manifest-hash"
    assert publication.validation_evidence == {"status": "passed"}

    bad_handle = manager.prepare_stage(
        "REQ-2",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/declared.js"],
    )
    (Path(bad_handle.path) / "backend" / "undeclared.js").write_text("bad;\n", encoding="utf-8")
    with pytest.raises(StagePublicationError, match="outside the declared write set"):
        _publish(manager, bad_handle, writes=["backend/declared.js"])


def test_stage_publication_does_not_capture_coordinator_arc_files(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage("REQ-1", STAGE_TEST_GENERATION, declared_write_set=[])
    local_arc = Path(handle.path) / ".arc"
    local_arc.mkdir()
    (local_arc / "processing_queue.json").write_text("stage-local\n", encoding="utf-8")

    publication = _publish(manager, handle, writes=[])

    assert publication.declared_write_set == ()
    assert "processing_queue.json" not in publication.changed_files
    assert not (repo / ".arc" / "processing_queue.json").exists()


def test_empty_stage_publication_integrates_without_creating_a_fake_merge_commit(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage("REQ-1", STAGE_TEST_GENERATION, declared_write_set=[])
    publication = _publish(manager, handle, writes=[])

    before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    committed, detail = manager.integrate_stage(handle, publication, "merge empty stage")

    assert committed is False
    assert "already at" in detail
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == before


def test_stage_merge_rebases_once_when_integration_advanced(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage(
        "REQ-1",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/feature.js"],
    )
    (Path(handle.path) / "backend" / "feature.js").write_text("feature;\n", encoding="utf-8")
    publication = _publish(manager, handle, writes=["backend/feature.js"])

    (repo / "backend" / "integration.js").write_text("integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration update"], repo)

    committed, detail = manager.integrate_stage(handle, publication, "merge stage")

    assert committed is True
    assert "rebase" in detail
    assert (repo / "backend" / "feature.js").exists()
    assert (repo / "backend" / "integration.js").exists()
    manager.settle_stage(handle, published=True)
    assert not Path(handle.path).exists()


def test_stage_merge_conflict_preserves_worktree_after_rebase_failure(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare_stage(
        "REQ-1",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/app.js"],
    )
    (Path(handle.path) / "backend" / "app.js").write_text("stage;\n", encoding="utf-8")
    publication = _publish(manager, handle, writes=["backend/app.js"])

    (repo / "backend" / "app.js").write_text("integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration conflict"], repo)

    with pytest.raises(MergeConflictError):
        manager.integrate_stage(handle, publication, "merge conflicting stage")

    assert manager._is_registered(Path(handle.path))
    assert (repo / "backend" / "app.js").read_text(encoding="utf-8") == "integration;\n"
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def _stage_task(node_id: str, stage: str, order: int, status: str) -> dict[str, object]:
    return {
        "stage_task_id": f"{node_id}:{stage}",
        "node_id": node_id,
        "stage": stage,
        "order": order,
        "node_order": order,
        "status": status,
        "applicable": True,
        "publication": {
            "base_commit": "base",
            "artifact_commit": f"artifact-{node_id}-{stage}",
            "declared_write_set": [],
            "contract_hash": "contract",
            "test_manifest_hash": "manifest",
            "validation_evidence": {"status": "passed"},
        },
    }


def test_merge_queue_waits_for_same_node_prerequisite_and_is_stable() -> None:
    interface = _stage_task("A", STAGE_INTERFACE_DESIGN, 1, STAGE_READY_TO_MERGE)
    test = _stage_task("A", STAGE_TEST_GENERATION, 2, STAGE_READY_TO_MERGE)
    other = _stage_task("B", STAGE_INTERFACE_DESIGN, 3, STAGE_READY_TO_MERGE)
    queue = {
        "stage_tasks": [
            _stage_task("A", STAGE_VISUAL_ANALYSIS, 0, STAGE_PUBLISHED),
            interface,
            test,
            _stage_task("B", STAGE_VISUAL_ANALYSIS, 4, STAGE_PUBLISHED),
            other,
        ],
        "tasks": [],
        "node_states": {"A": "UNSEEN", "B": "UNSEEN"},
        "node_design_done": {"A": False, "B": False},
        "parents": {},
        "descendants": {},
        "dependencies": {},
    }
    merge_queue = StageMergeQueue()
    merge_queue.enqueue(interface, interface["publication"])
    merge_queue.enqueue(test, test["publication"])
    merge_queue.enqueue(other, other["publication"])

    assert merge_queue.next_ready(queue).stage_task_id == interface["stage_task_id"]
    interface["status"] = STAGE_PUBLISHED
    assert merge_queue.next_ready(queue).stage_task_id == test["stage_task_id"]
    test["status"] = STAGE_PUBLISHED
    assert merge_queue.next_ready(queue).stage_task_id == other["stage_task_id"]


def test_stage_drain_runs_real_stage_worktrees_and_serial_merge_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo, _manager = _init_repo(tmp_path)

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return {"id": node_id, "children_ids": [], "name": node_id, "description": node_id}

        def list_interfaces(self, req_id: str) -> list[dict[str, object]]:
            return []

        def list_tests(self, req_id: str) -> list[dict[str, object]]:
            return []

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    class _Events:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    class _Runner:
        def __init__(self, workspace: str) -> None:
            self.workspace = Path(workspace)

        async def run_interface_design_stage(
            self, _node_id: str, _requirement: dict[str, object]
        ) -> bool:
            (self.workspace / "backend" / "stage-feature.js").write_text("feature;\n", encoding="utf-8")
            return True

        async def run_test_generation_stage(
            self, _node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return True

        async def run_implement_phase(self, _node_id: str, _requirement: dict[str, object]) -> bool:
            (self.workspace / "backend" / "stage-implementation.js").write_text(
                "implementation;\n", encoding="utf-8"
            )
            return True

    runtime = SimpleNamespace(traceability=_Traceability(), events=_Events())
    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4100,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    manager._build_task_phase_runner = lambda workspace, _port, handle=None: _Runner(workspace)
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"),
        {"id": "L", "name": "leaf", "description": "leaf", "children": []},
    )
    for task in queue["stage_tasks"]:
        stage = task["stage"]
        if stage == STAGE_VISUAL_ANALYSIS:
            task["status"] = STAGE_PUBLISHED
        elif stage == STAGE_INTERFACE_DESIGN:
            task["declared_write_set"] = ["backend/stage-feature.js"]
        elif stage == STAGE_TEST_GENERATION:
            task["declared_write_set"] = []
        elif stage == "IMPLEMENTATION":
            task["declared_write_set"] = ["backend/stage-implementation.js"]

    asyncio.run(manager._drain_stage_tasks(queue, lambda task: manager._execute_stage_task(task, queue)))

    assert (repo / "backend" / "stage-feature.js").exists()
    assert (repo / "backend" / "stage-implementation.js").exists()
    assert not list((repo / ".arc" / "stage-worktrees").iterdir())
    assert queue["node_states"]["L"] == "PASSED"


def test_stage_merge_queue_rehydrates_ready_publication_after_coordinator_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo, stage_manager = _init_repo(tmp_path)
    handle = stage_manager.prepare_stage(
        "R",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/recovered.js"],
    )
    (Path(handle.path) / "backend" / "recovered.js").write_text("recovered;\n", encoding="utf-8")
    publication = _publish(stage_manager, handle, writes=["backend/recovered.js"])

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return {"id": node_id, "children_ids": []}

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    class _Events:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    runtime = SimpleNamespace(traceability=_Traceability(), events=_Events())
    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4200,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"),
        {
            "id": "R",
            "name": "root",
            "description": "root",
            "children": [{"id": "L", "name": "leaf", "description": "leaf", "children": []}],
        },
    )
    root_visual = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:VISUAL_ANALYSIS")
    root_visual["status"] = STAGE_PUBLISHED
    root_interface = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:INTERFACE_DESIGN")
    root_interface["status"] = STAGE_READY_TO_MERGE
    root_interface["publication"] = publication.to_dict()
    root_test = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:TEST_GENERATION")
    root_test["status"] = "SKIPPED"

    asyncio.run(manager._drain_stage_tasks(queue, lambda _task: {"status": STAGE_PUBLISHED}))

    assert root_interface["status"] == STAGE_PUBLISHED
    assert queue["node_states"]["R"] == "DESIGNED"
    assert (repo / "backend" / "recovered.js").exists()
    assert not Path(handle.path).exists()


def test_failed_stage_does_not_publish_a_late_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo, _stage_manager = _init_repo(tmp_path)
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"),
        {"id": "L", "name": "leaf", "description": "leaf", "children": []},
    )
    for task in queue["stage_tasks"]:
        if task["stage"] in {STAGE_VISUAL_ANALYSIS, STAGE_INTERFACE_DESIGN}:
            task["status"] = STAGE_PUBLISHED
        elif task["stage"] == STAGE_TEST_GENERATION:
            task["declared_write_set"] = ["tests/test_l.py"]
    queue["node_states"]["L"] = "DESIGNING"
    stage_task = next(task for task in queue["stage_tasks"] if task["stage_task_id"] == "L:TEST_GENERATION")
    transition_stage_task(queue, "L", STAGE_TEST_GENERATION, STAGE_RUNNING)

    manager = ARCWorkflowManager(
        workspace_path=str(repo), requirement_path="", app_type="cli", web_port=4200,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = SimpleNamespace(traceability=SimpleNamespace(list_interfaces=lambda **_: [], list_tests=lambda **_: []))
    manager._save_processing_queue = lambda _queue: None

    async def _run_and_fail(_stage, current_queue, _requirement, runner) -> bool:
        path = Path(runner.workspace_path) / "tests" / "test_l.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_l(): assert False\n", encoding="utf-8")
        fail_stage_task(
            current_queue, "L", STAGE_TEST_GENERATION,
            error="no registered tests", error_category="test_generation",
        )
        return True

    manager._run_stage_phase_in_worktree = _run_and_fail
    before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    result = asyncio.run(manager._execute_stage_task_in_worktree(stage_task, queue, {"id": "L"}))
    asyncio.run(manager._drain_stage_merge_queue(queue))

    assert result["status"] == STAGE_FAILED
    assert stage_task["status"] == STAGE_FAILED
    assert stage_task["error"] == "no registered tests"
    assert queue["node_states"]["L"] == "FAILED"
    assert next(t for t in queue["stage_tasks"] if t["stage_task_id"] == "L:IMPLEMENTATION")["status"] == STAGE_BLOCKED
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == before
    assert not (repo / "tests" / "test_l.py").exists()
    assert len(manager._stage_merge_queue) == 0


def test_failed_queued_publication_is_discarded_before_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo, stage_manager = _init_repo(tmp_path)
    handle = stage_manager.prepare_stage("L", STAGE_TEST_GENERATION, declared_write_set=["tests/test_l.py"])
    path = Path(handle.path) / "tests" / "test_l.py"
    path.parent.mkdir(parents=True)
    path.write_text("def test_l(): assert False\n", encoding="utf-8")
    publication = _publish(stage_manager, handle, writes=["tests/test_l.py"])
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"),
        {"id": "L", "name": "leaf", "description": "leaf", "children": []},
    )
    for task in queue["stage_tasks"]:
        if task["stage"] in {STAGE_VISUAL_ANALYSIS, STAGE_INTERFACE_DESIGN}:
            task["status"] = STAGE_PUBLISHED
    stage_task = next(task for task in queue["stage_tasks"] if task["stage_task_id"] == "L:TEST_GENERATION")
    transition_stage_task(queue, "L", STAGE_TEST_GENERATION, STAGE_RUNNING)
    transition_stage_task(queue, "L", STAGE_TEST_GENERATION, STAGE_READY_TO_MERGE, publication=publication.to_dict())

    manager = ARCWorkflowManager(
        workspace_path=str(repo), requirement_path="", app_type="cli", web_port=4200,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager._save_processing_queue = lambda _queue: None
    manager._stage_merge_queue.enqueue(stage_task, publication, handle=handle)
    fail_stage_task(queue, "L", STAGE_TEST_GENERATION, error="no registered tests")
    before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    assert asyncio.run(manager._drain_stage_merge_queue(queue)) is False
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == before
    assert not (repo / "tests" / "test_l.py").exists()
    assert stage_task["status"] == STAGE_FAILED
    assert len(manager._stage_merge_queue) == 0


def test_failed_stage_reports_blocked_successors_in_compile_result(tmp_path: Path) -> None:
    queue = load_or_create_queue(
        str(tmp_path / ".arc" / "processing_queue.json"),
        {"id": "L", "name": "leaf", "description": "leaf", "children": []},
    )
    fail_stage_task(queue, "L", STAGE_TEST_GENERATION, error="no registered tests")

    result = ARCWorkflowManager._build_compile_result(queue)

    assert result["failed_nodes"] == ["L"]
    assert result["blocked_stages"] == ["L:IMPLEMENTATION (blocked by failed TEST_GENERATION stage)"]
    assert result["unvalidated_tasks"] == []
    assert task_status(queue, next(t for t in queue["tasks"] if t["task_id"] == "L:IMPLEMENT")) == TASK_FAILED


def test_approved_cross_node_overlap_windows_execute_in_stage_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A's TEST_GENERATION and B's INTERFACE_DESIGN co-run in real worktrees.

    The ADR 0008 approved window between two sibling leaves has to execute
    real stage work (not just pass the scheduler policy): each stage runs in
    its own worktree behind a disjoint declared write set, the two rendezvous
    inside their runners, and both publications land through the serial merge
    queue.
    """

    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    repo, _stage_manager = _init_repo(tmp_path)
    tree = {
        "id": "ROOT",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "A", "name": "a", "description": "a", "children": []},
            {"id": "B", "name": "b", "description": "b", "children": []},
        ],
    }
    requirements = {
        node_id: {"id": node_id, "children_ids": children}
        for node_id, children in (("ROOT", ["A", "B"]), ("A", []), ("B", []))
    }

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return dict(requirements[node_id])

        def list_interfaces(self, req_id: str) -> list[dict[str, object]]:
            return []

        def list_tests(self, req_id: str) -> list[dict[str, object]]:
            return []

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    class _Events:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    window_writes = {
        ("A", STAGE_INTERFACE_DESIGN): ["backend/a-feature.js"],
        ("A", STAGE_TEST_GENERATION): ["tests/a.test.js"],
        ("A", "IMPLEMENTATION"): ["backend/a-impl.js"],
        ("B", STAGE_INTERFACE_DESIGN): ["backend/b-feature.js"],
        ("B", STAGE_TEST_GENERATION): ["tests/b.test.js"],
        ("B", "IMPLEMENTATION"): ["backend/b-impl.js"],
    }
    rendezvous_keys = {
        ("A", STAGE_TEST_GENERATION),
        ("B", STAGE_INTERFACE_DESIGN),
    }
    arrived: set[tuple[str, str]] = set()
    both_here = asyncio.Event()
    peak = 0

    class _Runner:
        def __init__(self, workspace: str) -> None:
            self.workspace = Path(workspace)

        async def _run(self, node_id: str, stage: str) -> bool:
            nonlocal peak
            key = (node_id, stage)
            arrived.add(key)
            peak = max(peak, len(arrived))
            if key in rendezvous_keys:
                if rendezvous_keys.issubset(arrived):
                    both_here.set()
                # The window must co-run; a sequential drain times out here.
                await asyncio.wait_for(both_here.wait(), timeout=15)
            for relative in window_writes.get(key, []):
                path = self.workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{node_id}:{stage}\n", encoding="utf-8")
            arrived.discard(key)
            return True

        async def run_interface_design_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_INTERFACE_DESIGN)

        async def run_test_generation_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_TEST_GENERATION)

        async def run_implement_phase(self, node_id: str, _requirement: dict[str, object]) -> bool:
            return await self._run(node_id, "IMPLEMENTATION")

    runtime = SimpleNamespace(traceability=_Traceability(), events=_Events())
    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4300,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    manager._build_task_phase_runner = lambda workspace, _port, handle=None: _Runner(workspace)
    queue = load_or_create_queue(str(repo / ".arc" / "processing_queue.json"), tree)
    for task in queue["stage_tasks"]:
        stage = task["stage"]
        if stage == STAGE_VISUAL_ANALYSIS:
            task["status"] = STAGE_PUBLISHED
        else:
            task["declared_write_set"] = list(
                window_writes.get((str(task["node_id"]), stage), [])
            )

    asyncio.run(manager._drain_stage_tasks(queue, lambda task: manager._execute_stage_task(task, queue)))

    assert both_here.is_set(), "the approved window never co-ran"
    assert peak == 2
    for relative in window_writes.values():
        for path in relative:
            assert (repo / path).exists(), f"{path} never reached integration"
    assert not list((repo / ".arc" / "stage-worktrees").iterdir())
    assert queue["node_states"] == {"ROOT": "PASSED", "A": "PASSED", "B": "PASSED"}


def test_adjacent_test_generation_stages_execute_in_stage_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adjacent TestGenerator stages co-run and publish through the merge queue."""

    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    repo, _stage_manager = _init_repo(tmp_path)
    tree = {
        "id": "ROOT",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "A", "name": "a", "description": "a", "children": []},
            {"id": "B", "name": "b", "description": "b", "children": []},
        ],
    }
    requirements = {
        node_id: {"id": node_id, "children_ids": children}
        for node_id, children in (("ROOT", ["A", "B"]), ("A", []), ("B", []))
    }
    a_test = f"tests/generated/{stable_node_path_segment('A')}/unit/a.test.js"
    b_test = f"tests/generated/{stable_node_path_segment('B')}/unit/b.test.js"
    window_writes = {
        ("A", STAGE_INTERFACE_DESIGN): ["backend/a-feature.js"],
        ("A", STAGE_TEST_GENERATION): [a_test],
        ("A", "IMPLEMENTATION"): ["backend/a-impl.js"],
        ("B", STAGE_INTERFACE_DESIGN): ["backend/b-feature.js"],
        ("B", STAGE_TEST_GENERATION): [b_test],
        ("B", "IMPLEMENTATION"): ["backend/b-impl.js"],
    }
    rendezvous_keys = {
        ("A", STAGE_TEST_GENERATION),
        ("B", STAGE_TEST_GENERATION),
    }
    arrived: set[tuple[str, str]] = set()
    both_here = asyncio.Event()
    peak = 0

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return dict(requirements[node_id])

        def list_interfaces(self, req_id: str) -> list[dict[str, object]]:
            return []

        def list_tests(self, req_id: str) -> list[dict[str, object]]:
            return []

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    class _Events:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    class _Runner:
        def __init__(self, workspace: str) -> None:
            self.workspace = Path(workspace)

        async def _run(self, node_id: str, stage: str) -> bool:
            nonlocal peak
            key = (node_id, stage)
            arrived.add(key)
            peak = max(peak, len(arrived))
            if key in rendezvous_keys:
                if rendezvous_keys.issubset(arrived):
                    both_here.set()
                await asyncio.wait_for(both_here.wait(), timeout=15)
            for relative in window_writes.get(key, []):
                path = self.workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{node_id}:{stage}\n", encoding="utf-8")
            arrived.discard(key)
            return True

        async def run_interface_design_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_INTERFACE_DESIGN)

        async def run_test_generation_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_TEST_GENERATION)

        async def run_implement_phase(self, node_id: str, _requirement: dict[str, object]) -> bool:
            return await self._run(node_id, "IMPLEMENTATION")

    runtime = SimpleNamespace(traceability=_Traceability(), events=_Events())
    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4300,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    manager._build_task_phase_runner = lambda workspace, _port, handle=None: _Runner(workspace)
    queue = load_or_create_queue(str(repo / ".arc" / "processing_queue.json"), tree)
    for task in queue["stage_tasks"]:
        stage = task["stage"]
        node_id = str(task["node_id"])
        if stage == STAGE_VISUAL_ANALYSIS:
            task["status"] = STAGE_PUBLISHED
        else:
            task["declared_write_set"] = list(window_writes.get((node_id, stage), []))

    asyncio.run(manager._drain_stage_tasks(queue, lambda task: manager._execute_stage_task(task, queue)))

    assert both_here.is_set(), "adjacent TestGenerator stages did not co-run"
    assert peak == 2
    assert (repo / a_test).exists()
    assert (repo / b_test).exists()
    for relative in window_writes.values():
        for path in relative:
            assert (repo / path).exists(), f"{path} never reached integration"
    assert not list((repo / ".arc" / "stage-worktrees").iterdir())
    assert queue["node_states"] == {"ROOT": "PASSED", "A": "PASSED", "B": "PASSED"}


def test_distant_test_generation_stages_execute_in_stage_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TestGenerator stages two pre-order slots apart co-run through the merge queue.

    The window's safety rests on per-node test-namespace confinement, not on
    node-order adjacency: A and C are separated by B in the pre-order, and the
    capacity-2 drain must still co-run their generators.
    """

    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    repo, _stage_manager = _init_repo(tmp_path)
    tree = {
        "id": "ROOT",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "A", "name": "a", "description": "a", "children": []},
            {"id": "B", "name": "b", "description": "b", "children": []},
            {"id": "C", "name": "c", "description": "c", "children": []},
        ],
    }
    requirements = {
        node_id: {"id": node_id, "children_ids": children}
        for node_id, children in (("ROOT", ["A", "B", "C"]), ("A", []), ("B", []), ("C", []))
    }
    a_test = f"tests/generated/{stable_node_path_segment('A')}/unit/a.test.js"
    b_test = f"tests/generated/{stable_node_path_segment('B')}/unit/b.test.js"
    c_test = f"tests/generated/{stable_node_path_segment('C')}/unit/c.test.js"
    window_writes = {
        ("A", STAGE_INTERFACE_DESIGN): ["backend/a-feature.js"],
        ("A", STAGE_TEST_GENERATION): [a_test],
        ("A", "IMPLEMENTATION"): ["backend/a-impl.js"],
        ("B", STAGE_INTERFACE_DESIGN): ["backend/b-feature.js"],
        ("B", STAGE_TEST_GENERATION): [b_test],
        ("B", "IMPLEMENTATION"): ["backend/b-impl.js"],
        ("C", STAGE_INTERFACE_DESIGN): ["backend/c-feature.js"],
        ("C", STAGE_TEST_GENERATION): [c_test],
        ("C", "IMPLEMENTATION"): ["backend/c-impl.js"],
    }
    rendezvous_keys = {
        ("A", STAGE_TEST_GENERATION),
        ("C", STAGE_TEST_GENERATION),
    }
    arrived: set[tuple[str, str]] = set()
    both_here = asyncio.Event()
    peak = 0

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return dict(requirements[node_id])

        def list_interfaces(self, req_id: str) -> list[dict[str, object]]:
            return []

        def list_tests(self, req_id: str) -> list[dict[str, object]]:
            return []

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    class _Events:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    class _Runner:
        def __init__(self, workspace: str) -> None:
            self.workspace = Path(workspace)

        async def _run(self, node_id: str, stage: str) -> bool:
            nonlocal peak
            key = (node_id, stage)
            arrived.add(key)
            peak = max(peak, len(arrived))
            if key in rendezvous_keys:
                if rendezvous_keys.issubset(arrived):
                    both_here.set()
                # The distant window must co-run; a sequential drain times out here.
                await asyncio.wait_for(both_here.wait(), timeout=15)
            for relative in window_writes.get(key, []):
                path = self.workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{node_id}:{stage}\n", encoding="utf-8")
            arrived.discard(key)
            return True

        async def run_interface_design_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_INTERFACE_DESIGN)

        async def run_test_generation_stage(
            self, node_id: str, _requirement: dict[str, object]
        ) -> bool:
            return await self._run(node_id, STAGE_TEST_GENERATION)

        async def run_implement_phase(self, node_id: str, _requirement: dict[str, object]) -> bool:
            return await self._run(node_id, "IMPLEMENTATION")

    runtime = SimpleNamespace(traceability=_Traceability(), events=_Events())
    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4300,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    manager._build_task_phase_runner = lambda workspace, _port, handle=None: _Runner(workspace)
    queue = load_or_create_queue(str(repo / ".arc" / "processing_queue.json"), tree)
    for task in queue["stage_tasks"]:
        stage = task["stage"]
        node_id = str(task["node_id"])
        if stage == STAGE_VISUAL_ANALYSIS:
            task["status"] = STAGE_PUBLISHED
        else:
            task["declared_write_set"] = list(window_writes.get((node_id, stage), []))

    asyncio.run(manager._drain_stage_tasks(queue, lambda task: manager._execute_stage_task(task, queue)))

    assert both_here.is_set(), "distant TestGenerator stages did not co-run"
    assert peak == 2
    assert (repo / a_test).exists()
    assert (repo / b_test).exists()
    assert (repo / c_test).exists()
    for relative in window_writes.values():
        for path in relative:
            assert (repo / path).exists(), f"{path} never reached integration"
    assert not list((repo / ".arc" / "stage-worktrees").iterdir())
    assert queue["node_states"] == {"ROOT": "PASSED", "A": "PASSED", "B": "PASSED", "C": "PASSED"}


def test_stage_merge_never_lands_coordinator_files(tmp_path: Path) -> None:
    """The merge boundary is the last line of defense for ``.arc`` state.

    A publish-time guard blocks coordinator paths, but a stage branch that
    carries them anyway (stale branch state, a bypassed path) must fail the
    integration instead of writing ``.arc`` JSON into the shared workspace -
    otherwise concurrent stage worktrees could merge runtime state (ADR 0008).
    """

    repo, manager = _init_repo(tmp_path)
    traceability_file = repo / ".arc" / "traceability" / "requirements.json"
    traceability_file.parent.mkdir(parents=True)
    traceability_file.write_text('{"requirements": []}\n', encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "track coordinator traceability"], repo)

    handle = manager.prepare_stage(
        "REQ-1",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/feature.js", ".arc/traceability/requirements.json"],
    )
    stage_tree = Path(handle.path)
    (stage_tree / "backend" / "feature.js").write_text("feature;\n", encoding="utf-8")
    (stage_tree / ".arc" / "traceability" / "requirements.json").write_text(
        '{"hijacked": true}\n', encoding="utf-8"
    )
    _git(["add", "-A"], stage_tree)
    _git(["commit", "-q", "-m", "stage carries coordinator state"], stage_tree)
    artifact = _git(["rev-parse", "HEAD"], stage_tree).stdout.strip()
    publication = StagePublication(
        node_id="REQ-1",
        stage=STAGE_INTERFACE_DESIGN,
        base_commit=handle.base_commit,
        artifact_commit=artifact,
        declared_write_set=(".arc/traceability/requirements.json", "backend/feature.js"),
        contract_hash="contract-hash",
        test_manifest_hash="manifest-hash",
        validation_evidence={"status": "passed"},
        changed_files=(".arc/traceability/requirements.json", "backend/feature.js"),
        branch=handle.branch,
        worktree_path=handle.path,
    )

    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    with pytest.raises(StagePublicationError, match="coordinator files"):
        manager.integrate_stage(handle, publication, "merge hijacked stage")

    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
    assert traceability_file.read_text(encoding="utf-8") == '{"requirements": []}\n'
    assert Path(handle.path).exists()


def _recording_event_client(repo: Path) -> EventClient:
    return EventClient(RuntimePaths.from_env(project_dir=str(repo)))


def test_replayed_stage_publication_does_not_duplicate_completion_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the merge commit and the queue save leaves a
    READY_TO_MERGE publication whose merge already landed; the resume
    replay must finalize it exactly once, driven by the runner event log
    instead of re-appending the aggregate completion markers."""

    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    repo, stage_manager = _init_repo(tmp_path)
    handle = stage_manager.prepare_stage(
        "R",
        STAGE_INTERFACE_DESIGN,
        declared_write_set=["backend/recovered.js"],
    )
    (Path(handle.path) / "backend" / "recovered.js").write_text("recovered;\n", encoding="utf-8")
    publication = _publish(stage_manager, handle, writes=["backend/recovered.js"])
    # The merge already landed, but the queue still records READY_TO_MERGE:
    # the exact on-disk state after a crash before the PUBLISHED save.
    committed, _detail = stage_manager.integrate_stage(handle, publication, "merge R design")
    assert committed is True

    class _Traceability:
        def get_requirement(self, node_id: str) -> dict[str, object]:
            return {"id": node_id, "children_ids": []}

        def upsert_node_state(self, _node_id: str, _state: str) -> None:
            return None

    manager = ARCWorkflowManager(
        workspace_path=str(repo),
        requirement_path="",
        app_type="cli",
        web_port=4300,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(),
        events=_recording_event_client(repo),
    )
    manager._save_processing_queue = lambda _queue: None
    queue = load_or_create_queue(
        str(repo / ".arc" / "processing_queue.json"),
        {
            "id": "R",
            "name": "root",
            "description": "root",
            "children": [{"id": "L", "name": "leaf", "description": "leaf", "children": []}],
        },
    )
    root_visual = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:VISUAL_ANALYSIS")
    root_visual["status"] = STAGE_PUBLISHED
    root_interface = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:INTERFACE_DESIGN")
    root_interface["status"] = STAGE_READY_TO_MERGE
    root_interface["publication"] = publication.to_dict()
    root_test = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "R:TEST_GENERATION")
    root_test["status"] = "SKIPPED"

    def _completed_events() -> int:
        events_path = repo / ".arc" / "runner-events.jsonl"
        return sum(
            1
            for event in read_jsonl(events_path)
            if event.get("type") == "requirement_state"
            and event.get("node_id") == "R"
            and event.get("phase") == "design"
            and event.get("status") == "completed"
        )

    asyncio.run(manager._rehydrate_stage_merge_queue(queue))
    asyncio.run(manager._drain_stage_merge_queue(queue))

    assert root_interface["status"] == STAGE_PUBLISHED, root_interface.get("error")
    assert queue["node_states"]["R"] == "DESIGNED"
    assert _completed_events() == 1

    # Replay the same crash window once more: the event log is the ledger,
    # so the second finalization must not append a second completion event.
    root_interface["status"] = STAGE_READY_TO_MERGE
    asyncio.run(manager._rehydrate_stage_merge_queue(queue))
    asyncio.run(manager._drain_stage_merge_queue(queue))

    assert root_interface["status"] == STAGE_PUBLISHED, root_interface.get("error")
    assert _completed_events() == 1

    # A fresh attempt re-arms the emission: its design/running event makes
    # the completion marker novel again even though one was recorded before.
    manager.runtime.events.mark_design_started("R")
    root_interface["status"] = STAGE_READY_TO_MERGE
    asyncio.run(manager._rehydrate_stage_merge_queue(queue))
    asyncio.run(manager._drain_stage_merge_queue(queue))

    assert root_interface["status"] == STAGE_PUBLISHED, root_interface.get("error")
    assert _completed_events() == 2

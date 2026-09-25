"""Stage worktree lifecycle and serialized publication merge contracts."""

from __future__ import annotations

import subprocess
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

from core.queue_state import (
    STAGE_INTERFACE_DESIGN,
    STAGE_PUBLISHED,
    STAGE_READY_TO_MERGE,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    load_or_create_queue,
)
from core.workflow import ARCWorkflowManager
from core.stage_merge_queue import StageMergeQueue
from core.stage_worktree import (
    StagePublicationError,
    StageWorktreeManager,
)
from core.worktree import MergeConflictError


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

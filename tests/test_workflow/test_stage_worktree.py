"""Stage worktree lifecycle and serialized publication merge contracts."""

from __future__ import annotations

import subprocess
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient
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
    StagePublication,
    StagePublicationError,
    StageWorktreeManager,
)
from core.worktree import MergeConflictError
from tests.helpers.jsonl import read_jsonl


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

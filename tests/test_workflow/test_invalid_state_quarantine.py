"""Quarantine of invalid persisted stage/node states during load (issue #329).

A corrupted, unknown, or future status in ``processing_queue.json`` must
never read as schedulable pending work: the load-time quarantine records the
original value in ``invalid_state_entries``, the projections keep the work
unschedulable, and resume/retry fail with an actionable state-integrity
report instead of silently rerunning the stage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.queue_state import (
    PHASE_DESIGN,
    STAGE_INTERFACE_DESIGN,
    STAGE_PENDING,
    TASK_FAILED,
    TASK_PENDING,
    aggregate_phase_status,
    collect_invalid_state_entries,
    describe_invalid_state_entry,
    load_or_create_queue,
    save_queue,
    stage_status_of,
    task_status,
    transition_stage_task,
)
from core.scheduling import next_runnable_stage_task, next_runnable_task
from core.workflow import ARCWorkflowManager


def _tree() -> dict[str, object]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [],
    }


def _queue_path(tmp_path: Path) -> Path:
    return tmp_path / ".arc" / "processing_queue.json"


def _load(tmp_path: Path) -> dict[str, object]:
    return load_or_create_queue(str(_queue_path(tmp_path)), _tree())


def _stage_row(queue: dict[str, Any], stage_task_id: str) -> dict[str, Any]:
    return next(item for item in queue["stage_tasks"] if item["stage_task_id"] == stage_task_id)


# ---------------------------------------------------------------------------
# fresh load: quarantine records the original value, nothing is schedulable
# ---------------------------------------------------------------------------


def test_fresh_load_quarantines_unknown_node_state_and_preserves_the_value(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)
    queue["node_states"]["R"] = "ZOMBIE"
    save_queue(queue, str(_queue_path(tmp_path)))

    restored = _load(tmp_path)

    assert restored["invalid_state_entries"] == [
        {"kind": "node_state", "node_id": "R", "stage": None, "value": "ZOMBIE"}
    ]
    # The original value stays inspectable in the persisted map.
    assert restored["node_states"]["R"] == "ZOMBIE"


def test_fresh_load_quarantines_unknown_stage_status_and_preserves_the_value(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)
    _stage_row(queue, "R:INTERFACE_DESIGN")["status"] = "FUTURE_STAGE"
    save_queue(queue, str(_queue_path(tmp_path)))

    restored = _load(tmp_path)

    assert restored["invalid_state_entries"] == [
        {
            "kind": "stage_status",
            "node_id": "R",
            "stage": STAGE_INTERFACE_DESIGN,
            "value": "FUTURE_STAGE",
        }
    ]
    assert restored["node_states"]["R"] == "UNSEEN"
    # The row keeps its raw status; the projection does not normalize it to
    # pending, so the quarantined stage can never silently rerun.
    assert _stage_row(restored, "R:INTERFACE_DESIGN")["status"] == "FUTURE_STAGE"
    assert stage_status_of(restored, "R", STAGE_INTERFACE_DESIGN) == "FUTURE_STAGE"


def test_fresh_queue_without_unknown_values_records_no_quarantine_entries(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)

    assert queue["invalid_state_entries"] == []
    assert collect_invalid_state_entries(queue) == []


def test_quarantine_survives_save_and_reload_with_the_original_value(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)
    queue["node_states"]["R"] = "ZOMBIE"
    _stage_row(queue, "R:INTERFACE_DESIGN")["status"] = "FUTURE_STAGE"
    save_queue(queue, str(_queue_path(tmp_path)))

    first = _load(tmp_path)
    save_queue(first, str(_queue_path(tmp_path)))
    second = _load(tmp_path)

    assert second["invalid_state_entries"] == first["invalid_state_entries"]
    assert second["node_states"]["R"] == "ZOMBIE"
    assert _stage_row(second, "R:INTERFACE_DESIGN")["status"] == "FUTURE_STAGE"


def test_unknown_node_state_never_projects_to_schedulable_tasks(
    tmp_path: Path,
) -> None:
    """An unknown state reads FAILED like any other un-schedulable terminal
    state; only UNSEEN means unstarted. A legacy queue whose unknown state
    previously fell through to PENDING reran the node from scratch."""
    queue = _load(tmp_path)
    queue["node_states"]["R"] = "ZOMBIE"

    assert [task_status(queue, task) for task in queue["tasks"]] == [
        TASK_FAILED,
        TASK_FAILED,
    ]
    assert next_runnable_task(queue) is None


def test_unknown_stage_status_is_not_scheduled_by_the_stage_selector(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)
    _stage_row(queue, "R:INTERFACE_DESIGN")["status"] = "FUTURE_STAGE"

    assert next_runnable_stage_task(queue) is None
    assert aggregate_phase_status(queue, "R", PHASE_DESIGN) == TASK_FAILED


def test_legacy_seeding_maps_an_unknown_node_state_to_unschedulable_stage_rows(
    tmp_path: Path,
) -> None:
    """A pre-stage queue whose aggregate state is unknown seeds FAILED stage
    rows (not pending ones) and quarantines the state value."""
    path = _queue_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "root_id": "R",
                "tasks": [
                    {"task_id": "R:DESIGN", "node_id": "R", "phase": "DESIGN", "status": "PENDING"},
                    {
                        "task_id": "R:IMPLEMENT",
                        "node_id": "R",
                        "phase": "IMPLEMENT",
                        "status": "PENDING",
                    },
                ],
                "node_states": {"R": "ZOMBIE"},
            }
        ),
        encoding="utf-8",
    )

    restored = _load(tmp_path)

    assert restored["invalid_state_entries"] == [
        {"kind": "node_state", "node_id": "R", "stage": None, "value": "ZOMBIE"}
    ]
    assert stage_status_of(restored, "R", STAGE_INTERFACE_DESIGN) == TASK_FAILED
    assert next_runnable_stage_task(restored) is None


def test_transition_from_a_quarantined_stage_status_raises_a_clear_error(
    tmp_path: Path,
) -> None:
    queue = _load(tmp_path)
    _stage_row(queue, "R:INTERFACE_DESIGN")["status"] = "FUTURE_STAGE"

    with pytest.raises(ValueError, match="FUTURE_STAGE"):
        transition_stage_task(queue, "R", STAGE_INTERFACE_DESIGN, STAGE_PENDING)


def test_describe_invalid_state_entry_renders_each_kind() -> None:
    assert describe_invalid_state_entry(
        {"kind": "node_state", "node_id": "R", "stage": None, "value": "ZOMBIE"}
    ) == "node R has unknown persisted state 'ZOMBIE'"
    assert describe_invalid_state_entry(
        {
            "kind": "stage_status",
            "node_id": "R",
            "stage": STAGE_INTERFACE_DESIGN,
            "value": "FUTURE_STAGE",
        }
    ) == (
        "stage task R:INTERFACE_DESIGN has unknown persisted status 'FUTURE_STAGE'"
    )


# ---------------------------------------------------------------------------
# workflow: resume/retry report a state-integrity failure and stay non-success
# ---------------------------------------------------------------------------


class _Traceability:
    def __init__(self) -> None:
        self.requirements: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}

    def store_requirement_tree(self, tree: dict[str, Any]) -> None:
        self.requirements[str(tree.get("id", ""))] = tree

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state


class _Events:
    def __getattr__(self, _name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


class _Git:
    def commit(self, message: str) -> bool:
        return False

    def status_porcelain(self) -> str:
        return ""


def _manager(tmp_path: Path, logs: list[tuple[Any, ...]]) -> ARCWorkflowManager:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        log_cb=lambda *args, **kwargs: logs.append(args),
    )
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(),
        events=_Events(),
        git=_Git(),
    )
    return manager


def _write_corrupt_queue(tmp_path: Path) -> None:
    queue = _load(tmp_path)
    queue["node_states"]["R"] = "ZOMBIE"
    save_queue(queue, str(_queue_path(tmp_path)))


def _messages(logs: list[tuple[Any, ...]], status: str) -> list[str]:
    return [str(args[1]) for args in logs if len(args) > 2 and args[2] == status]


def test_resume_reports_state_integrity_failure_and_leaves_the_file_untouched(
    tmp_path: Path,
) -> None:
    _write_corrupt_queue(tmp_path)
    before = _queue_path(tmp_path).read_text(encoding="utf-8")
    logs: list[tuple[Any, ...]] = []
    manager = _manager(tmp_path, logs)

    result = asyncio.run(
        manager.compile_requirement_tree(dict(_tree()), resume_from_queue=True)
    )

    assert result["ok"] is False
    assert result["status"] == "FAIL"
    assert result["invalid_state_entries"] == [
        {"kind": "node_state", "node_id": "R", "stage": None, "value": "ZOMBIE"}
    ]
    errors = _messages(logs, "error")
    assert any("ZOMBIE" in message for message in errors)
    assert any("processing_queue.json" in message for message in errors)
    # The gate fires before any mutation: the corrupt file is exactly as the
    # user left it, original value included.
    assert _queue_path(tmp_path).read_text(encoding="utf-8") == before


def test_retry_failed_reports_state_integrity_failure(tmp_path: Path) -> None:
    _write_corrupt_queue(tmp_path)
    logs: list[tuple[Any, ...]] = []
    manager = _manager(tmp_path, logs)

    result = asyncio.run(
        manager.compile_requirement_tree(dict(_tree()), retry_failed=True)
    )

    assert result["ok"] is False
    assert result["invalid_state_entries"]
    assert any("ZOMBIE" in message for message in _messages(logs, "error"))


def test_retry_of_the_quarantined_node_reports_state_integrity_failure(
    tmp_path: Path,
) -> None:
    """An explicit --retry must not launder a corrupted state back into
    pending work via the reset path."""
    _write_corrupt_queue(tmp_path)
    logs: list[tuple[Any, ...]] = []
    manager = _manager(tmp_path, logs)

    result = asyncio.run(
        manager.compile_requirement_tree(dict(_tree()), retry_node_ids=["R"])
    )

    assert result["ok"] is False
    assert result["invalid_state_entries"]
    assert any("ZOMBIE" in message for message in _messages(logs, "error"))


def test_fresh_compile_quarantines_with_warnings_and_stays_non_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --resume the run proceeds around the quarantined work (it is
    never scheduled) but the warnings name the original values and the run
    cannot be accepted."""
    monkeypatch.setenv("ARC_AUTO_TDD_RETRY", "0")
    _write_corrupt_queue(tmp_path)
    logs: list[tuple[Any, ...]] = []
    manager = _manager(tmp_path, logs)

    result = asyncio.run(manager.compile_requirement_tree(dict(_tree())))

    assert result["ok"] is False
    assert result["invalid_state_entries"] == [
        {"kind": "node_state", "node_id": "R", "stage": None, "value": "ZOMBIE"}
    ]
    warnings = _messages(logs, "warning")
    assert any("ZOMBIE" in message for message in warnings)

    persisted = json.loads(_queue_path(tmp_path).read_text(encoding="utf-8"))
    assert persisted["invalid_state_entries"] == result["invalid_state_entries"]
    assert persisted["node_states"]["R"] == "ZOMBIE"


def test_build_compile_result_folds_quarantined_entries_into_non_success(
    tmp_path: Path,
) -> None:
    """A queue whose aggregate work reads COMPLETED but whose stage row is
    corrupted must still produce a non-success outcome."""
    queue = _load(tmp_path)
    queue["node_states"]["R"] = "PASSED"
    _stage_row(queue, "R:INTERFACE_DESIGN")["status"] = "FUTURE_STAGE"
    queue["invalid_state_entries"] = collect_invalid_state_entries(queue)

    result = ARCWorkflowManager._build_compile_result(queue)

    assert result["ok"] is False
    assert result["invalid_state_entries"] == [
        {
            "kind": "stage_status",
            "node_id": "R",
            "stage": STAGE_INTERFACE_DESIGN,
            "value": "FUTURE_STAGE",
        }
    ]

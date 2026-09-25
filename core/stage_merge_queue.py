"""Pure, deterministic ordering for ready stage publications."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from core.scheduling import stage_publication_dependencies_met
from core.stage_worktree import StagePublication


@dataclass(frozen=True)
class StageMergeEntry:
    """One ready publication waiting for coordinator integration."""

    stage_task_id: str
    node_id: str
    stage: str
    order: int
    publication: StagePublication | dict[str, Any]
    handle: Any = None


class StageMergeQueue:
    """A stable topological merge queue with no Git or persistence side effects."""

    def __init__(self) -> None:
        self._entries: dict[str, StageMergeEntry] = {}

    def enqueue(
        self,
        stage_task: Mapping[str, Any],
        publication: StagePublication | Mapping[str, Any],
        *,
        handle: Any = None,
    ) -> StageMergeEntry:
        stage_task_id = str(
            stage_task.get("stage_task_id")
            or f"{stage_task.get('node_id', '')}:{stage_task.get('stage', '')}"
        )
        if not stage_task_id.strip():
            raise ValueError("stage task id is required for merge queue entry")
        if isinstance(publication, StagePublication):
            stored_publication: StagePublication | dict[str, Any] = publication
        else:
            stored_publication = copy.deepcopy(dict(publication))
        entry = StageMergeEntry(
            stage_task_id=stage_task_id,
            node_id=str(stage_task.get("node_id") or ""),
            stage=str(stage_task.get("stage") or "").strip().upper(),
            order=int(stage_task.get("order", 0) or 0),
            publication=stored_publication,
            handle=handle,
        )
        self._entries[stage_task_id] = entry
        return entry

    def next_ready(self, queue_state: Mapping[str, Any]) -> StageMergeEntry | None:
        """Return the earliest entry whose stage prerequisites are published."""

        tasks_by_id = {
            str(task.get("stage_task_id") or f"{task.get('node_id', '')}:{task.get('stage', '')}"): task
            for task in queue_state.get("stage_tasks", []) or []
            if isinstance(task, Mapping)
        }
        candidates: list[StageMergeEntry] = []
        for entry in self._entries.values():
            task = tasks_by_id.get(entry.stage_task_id)
            if task is None:
                continue
            if str(task.get("status") or "").strip().upper() != "READY_TO_MERGE":
                continue
            if not stage_publication_dependencies_met(dict(queue_state), dict(task)):
                continue
            candidates.append(entry)
        return min(candidates, key=lambda item: (item.order, item.stage_task_id), default=None)

    def discard(self, stage_task_id: str) -> None:
        self._entries.pop(str(stage_task_id), None)

    def discard_inactive(self, queue_state: Mapping[str, Any]) -> list[StageMergeEntry]:
        """Drop queued publications whose persisted stage is no longer ready."""

        statuses = {
            str(task.get("stage_task_id") or f"{task.get('node_id', '')}:{task.get('stage', '')}"):
            str(task.get("status") or "").strip().upper()
            for task in queue_state.get("stage_tasks", []) or []
            if isinstance(task, Mapping)
        }
        discarded = [
            entry for entry in self._entries.values()
            if statuses.get(entry.stage_task_id) != "READY_TO_MERGE"
        ]
        for entry in discarded:
            self.discard(entry.stage_task_id)
        return discarded

    def contains(self, stage_task_id: str) -> bool:
        """Whether a publication is already represented in the in-memory queue."""

        return str(stage_task_id) in self._entries

    def pop_ready(self, queue_state: Mapping[str, Any]) -> StageMergeEntry | None:
        entry = self.next_ready(queue_state)
        if entry is not None:
            self._entries.pop(entry.stage_task_id, None)
        return entry

    def __len__(self) -> int:
        return len(self._entries)

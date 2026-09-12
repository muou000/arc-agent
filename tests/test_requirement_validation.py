"""Requirement files must describe a finite, unambiguous tree."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.files import load_requirements, validate_requirement_tree
from core.workflow import ARCWorkflowManager


def _write_requirement(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "requirements.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "ROOT", "children": "not-a-list"}, "children must be a list"),
        ({"id": "ROOT", "children": [{"id": "R1"}, "malformed"]}, "children must contain mappings"),
        ({"id": "ROOT", "children": [{"id": "R1"}, {"id": "R1"}]}, "Duplicate requirement id"),
    ],
)
def test_load_requirements_rejects_malformed_or_duplicate_children(
    tmp_path: Path, payload: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_requirements(_write_requirement(tmp_path, payload))


def test_load_requirements_rejects_recursive_yaml_alias(tmp_path: Path) -> None:
    path = tmp_path / "requirements.yaml"
    path.write_text(
        "id: ROOT\nchildren: &children\n  - id: R1\n    children: *children\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cycle"):
        load_requirements(path)


def test_validate_requirement_tree_rejects_pathological_nesting() -> None:
    """A pathologically deep tree must fail validation, not blow the recursion limit."""
    deep_tree: dict = {"id": "LEAF"}
    for index in range(200):
        deep_tree = {"id": f"L{index}", "children": [deep_tree]}

    with pytest.raises(ValueError, match="maximum depth"):
        validate_requirement_tree(deep_tree)


def test_compile_requirement_tree_rejects_invalid_tree_before_runtime_setup(tmp_path: Path) -> None:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        log_cb=lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        manager.compile_requirement_tree({"id": "ROOT", "children": [{"id": "R1"}, {"id": "R1"}]})
    )

    assert result == {"ok": False, "failed_nodes": []}


def test_compile_resume_requires_an_existing_compatible_queue(tmp_path: Path) -> None:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(traceability=SimpleNamespace(store_requirement_tree=lambda tree: None))

    result = asyncio.run(manager.compile_requirement_tree({"id": "ROOT"}, resume_from_queue=True))

    assert result == {"ok": False, "failed_nodes": []}

    manager.arc_dir = str(tmp_path / ".arc")
    manager.queue_path = str(tmp_path / ".arc" / "processing_queue.json")
    Path(manager.queue_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.queue_path).write_text(json.dumps({"root_id": "OTHER", "tasks": []}), encoding="utf-8")

    result = asyncio.run(manager.compile_requirement_tree({"id": "ROOT"}, resume_from_queue=True))

    assert result == {"ok": False, "failed_nodes": []}

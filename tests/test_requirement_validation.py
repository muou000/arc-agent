"""Requirement files must describe a finite, unambiguous tree."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from core.files import load_requirements
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


def test_compile_requirement_tree_rejects_invalid_tree_before_runtime_setup(tmp_path: Path) -> None:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        log_cb=lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        manager.compile_requirement_tree({"id": "ROOT", "children": [{"id": "R1"}, {"id": "R1"}]})
    )

    assert result == {"ok": False, "failed_nodes": []}

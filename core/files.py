from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml


def load_requirements(requirement_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(requirement_path)
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Requirement file must contain a mapping: {path}")
    if isinstance(payload.get("root"), dict):
        payload = payload["root"]
    if "id" not in payload and isinstance(payload.get("requirement"), dict):
        payload = payload["requirement"]
    if not str(payload.get("id", "")).strip():
        raise ValueError(f"Requirement root node id is missing: {path}")
    validate_requirement_tree(payload)
    return payload


MAX_REQUIREMENT_TREE_DEPTH = 64


def validate_requirement_tree(requirement_tree: dict[str, Any]) -> None:
    """Reject malformed, cyclic, or ambiguous nested requirement trees."""

    if not isinstance(requirement_tree, dict):
        raise ValueError("Requirement tree root must be a mapping.")

    seen_ids: set[str] = set()
    active_nodes: set[int] = set()

    def visit(node: object, location: str, depth: int) -> None:
        if depth > MAX_REQUIREMENT_TREE_DEPTH:
            raise ValueError(
                f"Requirement tree nesting exceeds the maximum depth of "
                f"{MAX_REQUIREMENT_TREE_DEPTH} ({location})."
            )
        if not isinstance(node, dict):
            raise ValueError(f"Requirement children must contain mappings ({location}).")

        node_identity = id(node)
        if node_identity in active_nodes:
            raise ValueError(f"Requirement tree contains a cycle ({location}).")
        active_nodes.add(node_identity)
        try:
            req_id = str(node.get("id") or node.get("req_id") or "").strip()
            if not req_id:
                raise ValueError(f"Requirement node id is missing ({location}).")
            if req_id in seen_ids:
                raise ValueError(f"Duplicate requirement id: {req_id}")
            seen_ids.add(req_id)

            children = node.get("children")
            if children is None:
                return
            if not isinstance(children, list):
                raise ValueError(f"Requirement children must be a list ({req_id}).")
            for index, child in enumerate(children):
                visit(child, f"{req_id}.children[{index}]", depth + 1)
        finally:
            active_nodes.remove(node_identity)

    visit(requirement_tree, "root", 1)


def read_json_file(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        with candidate.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_file(path: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    candidate = Path(path)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = candidate.with_suffix(candidate.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(candidate)

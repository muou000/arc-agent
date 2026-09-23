"""Prompt contract for the Requirement Snapshot JSON block (#211).

The snapshot embeds the full requirement row in the DESIGN and TestGenerator
user prompts (``task_context_block``) and in the green-baseline repair
message (``agents/test_generator.py``). It is rendered compact — one line,
no pretty-print newlines, no separator padding: #188 measured pretty-printing
as ~199 tok of pure format cost on a dense leaf, spend that carries no
semantic content and is cache-amplified on every round of a node session.
"""

from __future__ import annotations

import json

from agents.context.prompts.common import task_context_block


def _snapshot_json(block: str, *, blank_line_after_header: bool) -> str:
    header = "### Requirement Snapshot\n"
    if blank_line_after_header:
        header += "\n"
    start = block.index(header) + len(header) + len("```json\n")
    end = block.index("\n```", start)
    return block[start:end]


def test_requirement_snapshot_block_is_compact_json() -> None:
    requirement_data = {
        "id": "REQ-LEAF-1",
        "name": "Registration",
        "description": "Register an account.",
        "children_ids": [],
        "scenarios": [
            {
                "name": "Happy path",
                "steps": [{"keyword": "GIVEN", "content": "the signup page"}],
            }
        ],
    }
    block = task_context_block(
        node_id="REQ-LEAF-1",
        dynamic_context="",
        requirement_data=requirement_data,
    )

    json_text = _snapshot_json(block, blank_line_after_header=True)
    # Semantics are unchanged: the block still round-trips to the full row.
    assert json.loads(json_text) == requirement_data
    # Format is compact: no pretty-print newlines, no separator padding.
    assert "\n" not in json_text
    assert ", " not in json_text
    assert ": " not in json_text

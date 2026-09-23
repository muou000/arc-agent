"""Shared pins for the Requirement Snapshot JSON block (#211).

Both the stage task context (``task_context_block``) and the green-baseline
repair message (``agents/test_generator.py``) embed the same block; these
helpers let each pin assert one shared contract instead of a private
extractor per test file.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The stage context joins sections with a blank line; the repair message puts
# the fence on the next line. Both shapes are the same block.
SNAPSHOT_BLOCK_RE = re.compile(r"### Requirement Snapshot\n\n?```json\n(.*?)\n```", re.DOTALL)


def extract_snapshot_json(text: str) -> str:
    match = SNAPSHOT_BLOCK_RE.search(text)
    assert match, "Requirement Snapshot json block missing from text"
    return match.group(1)


def assert_compact_requirement_snapshot(text: str, requirement_data: dict[str, Any]) -> str:
    """Pin the #211 compact format and the zero-semantic round-trip; returns
    the extracted JSON text.

    Fixture string values must not contain ``", "`` or ``": "`` — those
    substrings are the separator-padding detectors. Raw newlines cannot occur
    inside compact JSON string values, so the newline check is always safe.
    """
    json_text = extract_snapshot_json(text)
    assert json.loads(json_text) == requirement_data
    assert "\n" not in json_text, "snapshot must be single-line compact JSON"
    assert ", " not in json_text and ": " not in json_text, "snapshot must use compact separators"
    return json_text

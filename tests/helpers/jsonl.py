"""Shared JSONL readers for runtime artefacts in tests."""

from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL artefact (e.g. ``.arc/runner-events.jsonl``) into dicts.

    A missing file reads as empty; blank lines are skipped.
    """

    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

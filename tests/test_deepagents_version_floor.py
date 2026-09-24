"""The deepagents floor in requirements.txt is part of the runtime contract.

deepagents 0.7.13 rendered read_file bodies with a ``1  alpha`` line-number
gutter; 0.7.15 restored verbatim rendering. Without a declared floor a fresh
venv silently resolves the gutter-rendering version, and the suite's verdict
depends on which interpreter happens to run it instead of on the code. The
upper bound exists because the adapters bind deepagents private internals
(``factory.py`` swaps the stock FilesystemMiddleware by name and imports
``deepagents._models``; ``filesystem_adapters.py`` imports the symlink guard
and the grep regex-literal helper) — a major-version rewrite of those
internals must not land through a routine ``pip install -r``.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The verbatim contract (tests/test_agents/test_permission_denied_hint.py)
# needs at least this version; anything older renders the line-number gutter.
_VERBATIM_FLOOR = (0, 7, 15)


def _deepagents_requirement() -> str:
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    matches = [line.strip() for line in lines if re.match(r"deepagents(\s|$|\[|>|<|=|!|~)", line.strip())]
    assert matches, "requirements.txt must declare deepagents"
    return matches[0]


def test_deepagents_requirement_declares_verbatim_floor() -> None:
    requirement = _deepagents_requirement()
    floor = re.search(r">=\s*([0-9.]+)", requirement)
    assert floor is not None, f"deepagents requirement must declare a >= floor: {requirement!r}"
    parsed = tuple(int(part) for part in floor.group(1).split("."))
    assert parsed >= _VERBATIM_FLOOR, (
        f"deepagents floor {floor.group(1)} is below the verbatim-rendering "
        f"version 0.7.15; a fresh venv would resolve a gutter-rendering read_file"
    )


def test_deepagents_requirement_declares_an_upper_bound() -> None:
    requirement = _deepagents_requirement()
    assert re.search(r"<\s*[0-9.]+", requirement), (
        f"deepagents requirement must cap the major line the adapters' "
        f"private-internals bindings are verified against: {requirement!r}"
    )

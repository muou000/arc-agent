"""Stage skill selection: deterministic safety floor only.

Skill choice is generic: every stage agent attaches the ``/skills/`` source and
the runtime skills middleware injects the full catalog (name + description +
path) into the system prompt, so each stage agent reads whichever ``SKILL.md``
files match its task through progressive disclosure. There is no per-node
planning agent and no keyword matching for optional skills.

This module only computes the deterministic safety floor that every stage
*requires* regardless of what the model later chooses:

- ``auth-session-consistency`` when the node/contract carries auth context.
- ``tdd-test-failure-repair`` once a previous implementation failure exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SKILLS_SOURCE = "/skills/"
_AUTH_TERMS = (
    "login",
    "log in",
    "sign in",
    "register",
    "registration",
    "logout",
    "log out",
    "session",
    "authenticated",
    "authentication",
    "authorization",
    "current user",
    "account state",
)


def _contains_term(value: object, terms: tuple[str, ...]) -> bool:
    """Return True when any term appears in the casefolded JSON/text dump."""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    normalized = text.casefold()
    return any(term in normalized for term in terms)


def interface_design_skills(requirement_data: dict[str, Any]) -> list[str]:
    """Auth safety floor required for leaf design work."""

    is_leaf = not requirement_data.get("children_ids")
    if is_leaf and has_auth_context(requirement_data):
        return available_skill_names(["auth-session-consistency"])
    return []


def test_generation_skills(requirement_data: dict[str, Any]) -> list[str]:
    """Auth safety floor required for test generation."""

    if has_auth_context(requirement_data):
        return available_skill_names(["auth-session-consistency"])
    return []


def implementation_skills(
    *,
    interface_contract: str,
    previous_failure_summary: str,
) -> list[str]:
    """Repair/auth safety floor required for implementation."""

    names: list[str] = []
    if previous_failure_summary.strip():
        names.append("tdd-test-failure-repair")
    if has_auth_context(interface_contract):
        names.append("auth-session-consistency")
    return available_skill_names(names)


def has_auth_context(value: object) -> bool:
    """Detect explicit authentication/session ownership without guessing from shell-only text."""

    return _contains_term(value, _AUTH_TERMS)


def available_skill_names(names: list[str]) -> list[str]:
    """Keep declared skills in order and omit missing instruction files."""

    root = Path(__file__).resolve().parents[2] / "skills"
    return [name for name in dict.fromkeys(names) if (root / name / "SKILL.md").is_file()]

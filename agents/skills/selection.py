"""Select stage skills: deterministic safety floor union model-planned extras.

Optional guidance skills are no longer keyword-matched. They come exclusively
from the model-driven planner (``agents/skills/planning.py``), which decides
per node which skill instruction files each stage agent should read. Only the
safety-critical floor stays deterministic here:

- ``auth-session-consistency`` when the node/contract carries auth context.
- ``tdd-test-failure-repair`` once a previous implementation failure exists.

When planning failed or the model returned nothing suitable, only the floor
is injected — there is deliberately no keyword fallback.
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


def interface_design_skills(
    requirement_data: dict[str, Any],
    extra_skills: list[str] | None = None,
) -> list[str]:
    """Auth safety floor for leaf design work union the model-planned extras."""

    names: list[str] = []
    is_leaf = not requirement_data.get("children_ids")
    if is_leaf and has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    names.extend(extra_skills or [])
    return available_skill_names(names)


def test_generation_skills(
    requirement_data: dict[str, Any],
    extra_skills: list[str] | None = None,
) -> list[str]:
    """Auth safety floor union the model-planned extras (incl. layer selection)."""

    names: list[str] = []
    if has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    names.extend(extra_skills or [])
    return available_skill_names(names)


def implementation_skills(
    *,
    interface_contract: str,
    previous_failure_summary: str,
    extra_skills: list[str] | None = None,
) -> list[str]:
    """Repair/auth safety floor union the model-planned extras."""

    names: list[str] = []
    if previous_failure_summary.strip():
        names.append("tdd-test-failure-repair")
    if has_auth_context(interface_contract):
        names.append("auth-session-consistency")
    names.extend(extra_skills or [])
    return available_skill_names(names)


def has_auth_context(value: object) -> bool:
    """Detect explicit authentication/session ownership without guessing from shell-only text."""

    return _contains_term(value, _AUTH_TERMS)


def available_skill_names(names: list[str]) -> list[str]:
    """Keep declared skills in order and omit missing instruction files."""

    root = Path(__file__).resolve().parents[2] / "skills"
    return [name for name in dict.fromkeys(names) if (root / name / "SKILL.md").is_file()]

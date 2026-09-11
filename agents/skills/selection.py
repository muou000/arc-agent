"""Select the small set of stage skills that ARC requires for one invocation."""

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

# React/frontend implementation signals: the node owns user-facing UI code.
_FRONTEND_TERMS = (
    "react",
    "component",
    "page",
    "form",
    "header",
    "navigation",
    "layout",
    "button",
    "input",
    "modal",
    "table",
    "list",
    "card",
    "frontend",
    "ui",
    "tailwind",
    "css",
    "style",
    "visual",
    "responsive",
    "accessib",
    "aria",
    "label",
    "selector",
)

# Visual/design signals: the node carries a screenshot or explicit styling work.
_VISUAL_TERMS = (
    "visual_reference",
    "screenshot",
    "image",
    "design",
    "style",
    "layout",
    "color",
    "typography",
    "spacing",
    "theme",
    "aesthetic",
    "brand",
)

# TDD/repair signals: the node is entering implementation with tests to satisfy.
_TDD_TERMS = (
    "test",
    "spec",
    "assert",
    "expect",
    "coverage",
    "scenario",
    "given",
    "when",
    "then",
    "vitest",
    "playwright",
    "e2e",
    "unit",
    "integration",
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
    """Return the relevant design skill and, for leaf auth work, its cross-cutting skill."""

    is_leaf = not requirement_data.get("children_ids")
    names = ["non-leaf-ui-only-design" if not is_leaf else "leaf-full-design"]
    if is_leaf and has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    # Non-leaf nodes are UI/composition-only; give them the design-taste skill so
    # the visual shell does not read as a generic template.
    if not is_leaf and _contains_term(requirement_data, _VISUAL_TERMS):
        names.append("frontend-design")
    # Leaf nodes with a visual reference also benefit from design guidance while
    # they materialize the UI skeleton.
    if is_leaf and _contains_term(requirement_data, _VISUAL_TERMS):
        names.append("frontend-design")
    return available_skill_names(names)


def test_generation_skills(requirement_data: dict[str, Any]) -> list[str]:
    """Return test selection guidance plus auth guidance only when the node needs it."""

    names = ["leaf-test-layer-selection"]
    if has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    # Scenario-driven E2E work benefits from stable-selector and a11y guidance so
    # generated tests use accessible names the implementation can satisfy.
    if _contains_term(requirement_data, _FRONTEND_TERMS):
        names.append("web-design-guidelines")
    return available_skill_names(names)


def implementation_skills(*, interface_contract: str, previous_failure_summary: str) -> list[str]:
    """Expose repair guidance only after a real failure, plus relevant auth guidance."""

    names: list[str] = []
    if previous_failure_summary.strip():
        names.append("tdd-test-failure-repair")
    if has_auth_context(interface_contract):
        names.append("auth-session-consistency")
    # React implementation work gets performance + composition guidance.
    if _contains_term(interface_contract, _FRONTEND_TERMS):
        names.append("vercel-react-best-practices")
        names.append("vercel-composition-patterns")
    # Any implementation pass with tests to satisfy gets the TDD discipline.
    if _contains_term(interface_contract, _TDD_TERMS):
        names.append("test-driven-development")
    return available_skill_names(names)


def has_auth_context(value: object) -> bool:
    """Detect explicit authentication/session ownership without guessing from shell-only text."""

    return _contains_term(value, _AUTH_TERMS)


def available_skill_names(names: list[str]) -> list[str]:
    """Keep declared skills in order and omit missing instruction files."""

    root = Path(__file__).resolve().parents[2] / "skills"
    return [name for name in dict.fromkeys(names) if (root / name / "SKILL.md").is_file()]

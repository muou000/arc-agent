"""Model-driven per-node skill planning.

Before a requirement node enters its DESIGN phase, one lightweight LLM call
reads the skill catalog plus this node's requirement snapshot and decides
which skill instruction files each stage agent (interface design, test
generation, implementation) should load for this node.

Contract notes:

- No feature flag: planning runs whenever a model is configured. An empty
  ``MODEL`` environment variable is a runtime precondition, not a switch.
- No keyword fallback: when planning fails or the model returns nothing
  suitable, no optional skills are injected. The deterministic safety floor
  (auth consistency, failure repair) lives in ``selection.py`` and is
  independent of this module.
- Prompt layout is cache-first: the skill catalog and task rules form a
  byte-stable system prefix shared by every node's planning call, so the
  provider prefix cache hits from the second node onward; the per-node
  requirement snapshot opens the user message.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.model.factory import create_arc_chat_model
from agents.skills.selection import available_skill_names
from core.sessions import load_node_session, merge_node_session

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
STAGE_KEYS = ("design", "test_generation", "implementation")
MAX_SKILLS_PER_STAGE = 3
PLANNER_AGENT_NAME = "SkillPlanner"


class SkillPlanResponse(BaseModel):
    """Validated shape of the planner model's JSON answer."""

    summary: str = ""
    design: list[str] = Field(default_factory=list)
    test_generation: list[str] = Field(default_factory=list)
    implementation: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _skill_catalog_entries() -> tuple[tuple[str, str], ...]:
    """Parse every skills/*/SKILL.md frontmatter into (name, description)."""

    entries: list[tuple[str, str]] = []
    for skill_dir in sorted(SKILLS_ROOT.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        meta = _parse_frontmatter(skill_md)
        name = str(meta.get("name") or "").strip() or skill_dir.name
        description = " ".join(str(meta.get("description") or "").split())
        entries.append((name, description))
    return tuple(entries)


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def build_skill_catalog() -> str:
    """Byte-stable catalog text: the cacheable prefix of every planning call."""

    lines = []
    for name, description in _skill_catalog_entries():
        line = f"- {name}: {description}".rstrip()
        lines.append(line)
    return "\n".join(lines)


def _system_prompt() -> str:
    return "\n\n".join(
        [
            "You are the ARC skill planner. A requirement node is about to enter "
            "its DESIGN phase; decide which skill instruction files each stage "
            "agent should read for this node.",
            "Available skills:\n" + build_skill_catalog(),
            "\n".join(
                [
                    "Rules:",
                    "- Choose only skills that materially help this node; prefer fewer skills.",
                    "- At most 3 names per stage; an empty list is valid when nothing helps.",
                    "- Copy every name exactly from the available skills list.",
                    '- Respond with a single JSON object and nothing else: {"summary": "...", '
                    '"design": ["..."], "test_generation": ["..."], "implementation": ["..."]}',
                ]
            ),
        ]
    )


def _user_prompt(node_id: str, requirement_data: dict[str, Any]) -> str:
    snapshot = json.dumps(requirement_data, ensure_ascii=False, default=str)
    return f"Requirement snapshot:\n{snapshot}\n\nNode id: {node_id}"


async def plan_stage_skills(
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    log_cb: LogCallback | None = None,
    model: Any = None,
) -> dict[str, Any] | None:
    """Plan stage skills for one node. Returns None when no model is available
    or anything about the call/parse/validation fails (no fallback, no retry)."""

    if model is None:
        model_name = os.environ.get("MODEL", "").strip()
        if not model_name:
            return None
        model = create_arc_chat_model(model_name)
    try:
        response = await model.ainvoke(
            [
                SystemMessage(content=_system_prompt()),
                HumanMessage(content=_user_prompt(node_id, requirement_data)),
            ]
        )
        payload = _parse_plan_json(_extract_text(response))
        plan = SkillPlanResponse.model_validate(payload)
    except Exception as exc:
        await _log(
            log_cb,
            f"Skill planning failed for {node_id}: {type(exc).__name__}: {exc}. "
            "No optional skills will be injected.",
            "warning",
            node_id,
        )
        return None
    validated, unknown_names = _validate_plan(plan)
    if unknown_names:
        await _log(
            log_cb,
            f"Ignored unknown skill names from planner: {', '.join(sorted(unknown_names))}",
            "warning",
            node_id,
        )
    await _log(
        log_cb,
        "Planned stage skills: "
        + "; ".join(
            f"{stage}=[{', '.join(validated[stage]) or 'none'}]" for stage in STAGE_KEYS
        ),
        None,
        node_id,
    )
    return validated


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _parse_plan_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner response contained no JSON object")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("planner response JSON was not an object")
    return payload


def _validate_plan(plan: SkillPlanResponse) -> tuple[dict[str, Any], list[str]]:
    """Dedupe, drop unknown names, and cap each stage; return plan + dropped names."""

    known = {name for name, _ in _skill_catalog_entries()}
    validated: dict[str, Any] = {"summary": plan.summary.strip()}
    unknown_names: list[str] = []
    for stage in STAGE_KEYS:
        names = [str(item or "").strip() for item in getattr(plan, stage)]
        names = [name for name in names if name]
        unknown_names.extend(name for name in names if name not in known)
        validated[stage] = available_skill_names(names)[:MAX_SKILLS_PER_STAGE]
    return validated, unknown_names


async def plan_and_store_stage_skills(
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> dict[str, Any] | None:
    """Plan once per node and persist the result in the node session.

    The plan depends only on requirement text, so resume and every retry path
    reuse the stored plan instead of spending another model call.
    """

    existing = load_skill_plan(node_id)
    if existing is not None:
        await _log(log_cb, f"Reusing stored skill plan for {node_id}.", None, node_id)
        return existing
    plan = await plan_stage_skills(
        node_id=node_id,
        requirement_data=requirement_data,
        log_cb=log_cb,
    )
    if plan is None:
        return None
    merge_node_session(node_id, {"skill_plan": plan})
    return plan


def load_skill_plan(node_id: str) -> dict[str, Any] | None:
    plan = load_node_session(node_id).get("skill_plan")
    return plan if isinstance(plan, dict) else None


def load_skill_plan_extras(node_id: str, stage: str) -> list[str] | None:
    """Model-planned skills for one stage; None when no plan was stored."""

    plan = load_skill_plan(node_id)
    if plan is None:
        return None
    names = plan.get(stage)
    if not isinstance(names, list):
        return []
    return [str(name) for name in names]


async def _log(
    log_cb: LogCallback | None,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(PLANNER_AGENT_NAME, message, status, node_id)
    if hasattr(result, "__await__"):
        await result

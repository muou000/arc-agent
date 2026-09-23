"""Measurement for #188: token share of the duplicated requirement content.

DESIGN (InterfaceDesigner) and TestGenerator user prompts embed the
requirement twice: the ``<requirement_focus>`` digest built by the context
pipeline (``agents/context/pipeline.py``) and the full ``### Requirement
Snapshot`` JSON embedded by ``task_context_block``
(``agents/context/prompts/common.py``). This test seeds real requirement
nodes from the ``arc-bench-test`` trees into a real traceability store,
renders the real system + user prompts through the production builders (no
model calls), and measures the token share of both blocks.

Counting uses ``usage_capture._count_tokens`` — tiktoken ``cl100k_base`` when
the encoding data is available, the chars/4 heuristic otherwise — the same
accounting the usage fallback uses. Printed numbers are therefore
approximations of any specific provider's tokenizer; shares are what matter.
Offline runs that degrade to the heuristic still pass: assertions are
structural only. The recorded numbers live in issue #188.

Deliberate caveats kept honest in the measurements:

- one node per test invocation (the requirement tables of two seeded trees
  share ids like ``REQ-2`` and ``store_requirement_tree`` replaces the whole
  table, so a shared store would mix requirements across blocks);
- the workspace under test is empty, so the denominator lacks the scaffold
  files, workspace map, and interface cards a real node sees; measured shares
  are upper bounds;
- the visual reference is attached through the production path
  (``update_requirement_fields``), which stringifies dict payloads on
  persist, so the focus digest's ``<visual_reference>`` block stays empty and
  the analysis text reaches the model only inside the snapshot - exactly as
  in a real run. The dict-shaped variant is measured separately as the
  "intended digest shape" contrast via a monkeypatched requirement row.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from agents.context.pipeline import ContextPipeline
from agents.context.prompts.interface_designer import (
    get_system_prompt as design_system_prompt,
    get_user_prompt as design_user_prompt,
)
from agents.context.prompts.test_driven_developer import (
    get_system_prompt as tdd_system_prompt,
    get_user_prompt as tdd_user_prompt,
)
from agents.context.prompts.test_generator import (
    get_system_prompt as tg_system_prompt,
    get_user_prompt as tg_user_prompt,
)
from agents.model.usage_capture import _count_tokens
from core.files import load_requirements

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EASY_YAML = REPO_ROOT / "arc-bench-test" / "easy-ticketbooking" / "requirements" / "requirements.yaml"
KEEP_YAML = REPO_ROOT / "arc-bench-test" / "keep" / "requirements" / "requirements.yaml"
TRAIN_YAML = REPO_ROOT / "arc-bench-test" / "12306" / "requirements" / "requirements.yaml"

# A realistic visual-reference analysis: the vision prompt
# (core/visual_analysis.py) asks for layout hierarchy, section composition,
# component appearance, typography/colors, spacing, and data display, and
# production outputs land in the 1.5-3k character range. Synthetic because no
# real analysis cache ships with the repo; the length is the point, not the
# wording.
SYNTHETIC_VISUAL_ANALYSIS = """Layout hierarchy: top fixed navbar (64px) with brand mark left, primary nav center, and a right-aligned auth cluster; below it a two-column hero where the left column stacks a 32px headline, a 16px subheading, and two CTA buttons (primary filled, secondary outlined), while the right column holds a product screenshot card with 24px rounded corners and a soft drop shadow. Main content is a 12-column grid with 24px gutters and a max width of 1200px centered.

Section composition: after the hero, a three-card feature row (equal widths, 16px internal padding, 1px light border, 12px radius); then a full-width data table section with a sticky header row, zebra striping at 4% opacity, and a right-aligned actions column; footer is a single-line muted bar with left copyright and right link group.

Component appearance: buttons are 40px tall with 8px horizontal padding and 6px radius; inputs share the same radius and a 1px border that darkens on focus with a 2px accent outline. Primary accent is a saturated blue (#2563eb) with white text; destructive actions use red (#dc2626). Secondary text is 60% gray.

Typography: sans-serif family throughout; page title 32px/600, section titles 20px/600, body 14px/400, table and helper text 13px; line height 1.5.

Spacing and alignment: sections separated by 48px vertical rhythm, cards by 24px; labels sit above inputs with 6px gap; all forms are left-aligned in a 420px column.

Data presentation: empty states show a centered 48px icon with a one-line hint and a primary action; loading states replace table rows with shimmer placeholders; numeric columns are tabular-figures and right-aligned."""

FOCUS_RE = re.compile(r"<requirement_focus>.*?</requirement_focus>", re.DOTALL)
SNAPSHOT_RE = re.compile(r"### Requirement Snapshot\n\n```json\n(.*?)\n```", re.DOTALL)

MEASURED_NODES = (
    # (label, tree yaml, node id, attached visual analysis)
    # Dense realistic leaf: 4 scenarios + production-path visual reference
    # (the issue's requested shape).
    ("easy-ticketbooking/REQ-2+dense-visual", EASY_YAML, "REQ-2", SYNTHETIC_VISUAL_ANALYSIS),
    # Typical benchmark leaf: 4 scenarios, Chinese validation contract, no visual.
    ("12306/REQ-4.3.6+typical", TRAIN_YAML, "REQ-4.3.6", None),
    # Median-shaped leaf: small English node, one scenario, no visual.
    ("keep/REQ-2.6.2+median", KEEP_YAML, "REQ-2.6.2", None),
)


@pytest.fixture
def pipeline(arc_runtime, tmp_project_dir: Path) -> ContextPipeline:
    instance = ContextPipeline()
    instance.set_runtime(arc_runtime)
    instance.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    return instance


def _seed_node(arc_runtime, tree: dict, node_id: str, visual_analysis: str | None) -> dict[str, Any]:
    """Seed the store exactly like production: tree persist, then the visual
    precompute path's ``update_requirement_fields`` attachment (which
    stringifies dict payloads on persist)."""

    arc_runtime.traceability.store_requirement_tree(tree)
    if visual_analysis:
        arc_runtime.traceability.update_requirement_fields(
            node_id,
            visual_reference=[{"image_path": "./reference/login.png", "analysis": visual_analysis}],
        )
    return arc_runtime.traceability.get_requirement(node_id) or {}


def _build_session_context(pipeline: ContextPipeline, node_id: str, agent_type: str, workspace: Path) -> str:
    static_context, dynamic_context = pipeline.build_agent_context_split(
        node_id=node_id,
        agent_type=agent_type,
        map_workspace_dir=str(workspace),
    )
    return "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())


def _render_prompts(pipeline: ContextPipeline, node_id: str, requirement_data: dict[str, Any], workspace: Path) -> dict[str, tuple[str, str]]:
    """{stage: (system, user)} as the adapters assemble them."""

    design_context = _build_session_context(pipeline, node_id, "InterfaceDesigner", workspace)
    design = (
        design_system_prompt(),
        design_user_prompt(
            node_id=node_id,
            requirement_data=requirement_data,
            dynamic_context=design_context,
        ),
    )
    testgen_context = _build_session_context(pipeline, node_id, "TestGenerator", workspace)
    testgen = (
        tg_system_prompt(),
        tg_user_prompt(
            node_id=node_id,
            requirement_data=requirement_data,
            dynamic_context=testgen_context,
            interface_contract="",
        ),
    )
    tdd_context = _build_session_context(pipeline, node_id, "TestDrivenDeveloper", workspace)
    tdd = (
        tdd_system_prompt(),
        tdd_user_prompt(
            node_id=node_id,
            dynamic_context=tdd_context,
            interface_contract="",
            test_files=["frontend/src/features/login/Login.test.tsx"],
            test_type="E2E",
            node_tests=[
                {
                    "test_id": f"{node_id}-e2e-login",
                    "req_id": node_id,
                    "coverage_scope": "owned",
                    "interface_ids": [],
                    "type": "E2E",
                    "file_path": "frontend/src/features/login/Login.test.tsx",
                    "first_line": "import { test, expect } from '@playwright/test';",
                }
            ],
        ),
    )
    return {"DESIGN": design, "TestGenerator": testgen, "TestDrivenDeveloper": tdd}


def _requirement_core_tokens(requirement_data: dict[str, Any]) -> int:
    """Tokens of the requirement text itself: description plus scenario names
    and step contents - the payload both blocks carry."""

    total = _count_tokens(str(requirement_data.get("description", "")), "")
    for scenario in requirement_data.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        total += _count_tokens(str(scenario.get("name", "")), "")
        for step in scenario.get("steps") or []:
            if isinstance(step, dict):
                total += _count_tokens(str(step.get("content", "")), "")
    return total


def _measure(stage: str, node_label: str, system: str, user: str, requirement_data: dict[str, Any]) -> dict[str, float]:
    total = _count_tokens(system, "") + _count_tokens(user, "")
    focus_match = FOCUS_RE.search(user)
    snapshot_match = SNAPSHOT_RE.search(user)
    assert focus_match, f"{node_label}/{stage}: <requirement_focus> missing from user prompt"
    focus_tokens = _count_tokens(focus_match.group(0), "")
    snapshot_tokens = 0
    if stage in {"DESIGN", "TestGenerator"}:
        assert snapshot_match, f"{node_label}/{stage}: Requirement Snapshot block missing"
        # Round-trip proves the extracted block is the full requirement row.
        assert json.loads(snapshot_match.group(1)) == requirement_data
        snapshot_tokens = _count_tokens(snapshot_match.group(0), "")
    else:
        assert snapshot_match is None, f"{node_label}/{stage}: snapshot block must stay TDD-absent"
    combined = focus_tokens + snapshot_tokens
    core = _requirement_core_tokens(requirement_data)
    row = {
        "total": float(total),
        "focus": float(focus_tokens),
        "snapshot": float(snapshot_tokens),
        "combined": float(combined),
        "core": float(core),
        "focus_share": focus_tokens / total,
        "snapshot_share": snapshot_tokens / total if snapshot_tokens else 0.0,
        "combined_share": combined / total,
        "core_share": core / total,
    }
    for name in ("focus_share", "combined_share", "core_share"):
        assert 0.0 < row[name] < 1.0, (node_label, stage, name, row[name])
    if snapshot_tokens:
        assert 0.0 < row["snapshot_share"] < 1.0, (node_label, stage, row["snapshot_share"])
    print(
        f"[#188] {node_label} / {stage}: total={row['total']:.0f}tok "
        f"focus={row['focus']:.0f} ({row['focus_share'] * 100:.1f}%) "
        f"snapshot={row['snapshot']:.0f} ({row['snapshot_share'] * 100:.1f}%) "
        f"combined={row['combined']:.0f} ({row['combined_share'] * 100:.1f}%) "
        f"duplicated_core={row['core']:.0f} ({row['core_share'] * 100:.1f}%)"
    )
    return row


@pytest.mark.parametrize(("label", "yaml_path", "node_id", "visual_analysis"), MEASURED_NODES)
def test_measure_requirement_snapshot_token_share(
    arc_runtime,
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
    label: str,
    yaml_path: Path,
    node_id: str,
    visual_analysis: str | None,
) -> None:
    workspace = tmp_project_dir.resolve()
    tree = load_requirements(yaml_path)
    requirement_data = _seed_node(arc_runtime, tree, node_id, visual_analysis)
    assert requirement_data["scenarios"], f"seeded {node_id} must carry scenarios"

    rows = [
        (stage, _measure(stage, label, system, user, requirement_data))
        for stage, (system, user) in _render_prompts(pipeline, node_id, requirement_data, workspace).items()
    ]

    # Keep the measurement observable without pinning provider-specific token
    # counts: the snapshot-bearing stages must outweigh the TDD contrast
    # (focus only, no snapshot).
    by_stage = dict(rows)
    assert by_stage["DESIGN"]["combined"] > by_stage["TestDrivenDeveloper"]["combined"]


def test_measure_dict_shaped_visual_reference_variant(
    arc_runtime,
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "intended digest shape" contrast: had dict-shaped visual references
    reached the pipeline, the focus digest would carry the analysis too. The
    production persist path stringifies them, so this variant is produced by
    serving the dict row from the store directly."""

    workspace = tmp_project_dir.resolve()
    tree = load_requirements(EASY_YAML)
    requirement_data = _seed_node(arc_runtime, tree, "REQ-2", None)
    dict_references = [{"image_path": "./reference/login.png", "analysis": SYNTHETIC_VISUAL_ANALYSIS}]
    requirement_data = {**requirement_data, "visual_reference": dict_references}

    real_get_requirement = arc_runtime.traceability.get_requirement

    def dict_shaped_get_requirement(req_id: str) -> dict[str, Any] | None:
        row = real_get_requirement(req_id)
        if row is not None and str(row.get("req_id")) == "REQ-2":
            return {**row, "visual_reference": dict_references}
        return row

    monkeypatch.setattr(arc_runtime.traceability, "get_requirement", dict_shaped_get_requirement)

    label = "easy-ticketbooking/REQ-2+dict-visual"
    for stage, (system, user) in _render_prompts(pipeline, "REQ-2", requirement_data, workspace).items():
        row = _measure(stage, label, system, user, requirement_data)
        if stage == "DESIGN":
            # The digest now carries the analysis, so the focus block must
            # grow past the stringified-shape baseline.
            assert row["focus"] > row["snapshot"] * 0.2


def test_requirement_focus_digest_repeats_snapshot_scenario_text(
    arc_runtime,
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    """Structural pin behind the measurement: the focus block's scenario
    digest and the snapshot carry the same scenario text, so any compression
    scheme must pick one side."""

    workspace = tmp_project_dir.resolve()
    tree = load_requirements(EASY_YAML)
    data = _seed_node(arc_runtime, tree, "REQ-2", None)

    context_text = _build_session_context(pipeline, "REQ-2", "InterfaceDesigner", workspace)
    focus_match = FOCUS_RE.search(context_text)
    assert focus_match
    focus_block = focus_match.group(0)
    assert "<scenarios>" in focus_block
    # The focus digest flattens steps to "KEYWORD: content" lines; every
    # scenario name from the requirement row reappears inside it.
    for scenario in data["scenarios"]:
        assert scenario["name"] in focus_block

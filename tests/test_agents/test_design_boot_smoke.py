"""The DESIGN boot smoke: non-bootable skeletons fail inside the designing session.

The 2026-09-26 ticketbooking arc-output2 run failed REQ-1 at the *merge*
health gate: the design skeleton exported named handler functions while the
app.js glue mounted it as a router, Express 5 threw at module load, and the
node died terminally long after the designing agent's session could still
have repaired it. The stage-finish boot smoke probes the workspace the pass
just wrote and, on failure, spends a bounded budget of same-thread repair
asks carrying the backend's own process output before failing the stage
locally with a distinct failure category.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest

from agents.interface_designer import (
    DESIGN_BOOT_SMOKE_REPAIR_ROUNDS,
    DesignBootSmokeError,
    InterfaceDesigner,
)
from tests.helpers.faux import FauxChatModel, faux_tool_call

NODE_ID = "REQ-1"
PAGE_PATH = "frontend/src/pages/RegisterPage.tsx"

PAGE = """import { useState } from 'react';

// REQ-1 registration page skeleton.
function RegisterPage() {
  return <form data-testid="register-form" />;
}

export default RegisterPage;
"""

BOOT_FAILURE = (
    "backend runtime failed to start on the merged workspace\n"
    "=== Backend Process Output ===\n"
    "STDERR: TypeError: argument handler must be a function"
)

INTERFACE_ROW = {
    "interface_id": "REQ-1-UI-RegisterPage",
    "type": "UI",
    "req_id": NODE_ID,
    "name": "RegisterPage",
    "file_path": PAGE_PATH,
    "first_line": "import { useState } from 'react';",
    "responsibility": "Registration page skeleton.",
    "specification": "Renders the registration form shell.",
    "relation": "owned",
}


class ScriptedProbe:
    """Injected boot probe: scripted outcomes, recorded (workspace, port) calls."""

    def __init__(self, outcomes: list[str | None]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, workspace_root: str, port: int) -> str | None:
        self.calls.append((workspace_root, port))
        if not self._outcomes:
            return None
        return self._outcomes.popleft()


class FailingProbe:
    """A probe that must never be called; its call fails the test."""

    async def __call__(self, workspace_root: str, port: int) -> str | None:
        raise AssertionError("boot probe must not be called for this pass")


def _seed_backend(tmp_project_dir: Path) -> None:
    backend = tmp_project_dir / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/index.js"}}\n',
        encoding="utf-8",
    )


def _seed_requirement(arc_runtime) -> None:
    arc_runtime.traceability.store_requirement_tree(
        {"id": NODE_ID, "name": "注册", "description": "注册功能"}
    )


def _main_pass_responses(with_writes: bool = True) -> list:
    responses: list = []
    if with_writes:
        responses.append(
            faux_tool_call(
                "write_file",
                {"file_path": f"/workspace/{PAGE_PATH}", "content": PAGE},
                call_id="w0",
            )
        )
    responses.append(
        faux_tool_call(
            "InterfaceDesignResponse",
            {
                "summary": "REQ-1 registration skeleton.",
                "interfaces": [dict(INTERFACE_ROW)],
                "files_written": [PAGE_PATH] if with_writes else [],
            },
            call_id="final",
        )
    )
    return responses


def _repair_response(round_index: int):
    return faux_tool_call(
        "InterfaceDesignResponse",
        {
            "summary": f"Fixed the router export shape (round {round_index}).",
            "interfaces": [dict(INTERFACE_ROW)],
            "files_written": [PAGE_PATH],
        },
        call_id=f"boot-repair-{round_index}",
    )


def _make_designer(
    tmp_project_dir: Path, model: FauxChatModel, probe
) -> InterfaceDesigner:
    return InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        boot_probe=probe,
    )


def _call_text(call_messages: list) -> str:
    return "\n".join(str(getattr(message, "content", "")) for message in call_messages)


def test_boot_smoke_failure_repairs_in_session_and_passes(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """One failed probe, one same-thread repair ask carrying the backend output."""

    _seed_requirement(arc_runtime)
    _seed_backend(tmp_project_dir)
    probe = ScriptedProbe([BOOT_FAILURE, None])
    model = FauxChatModel(
        responses=[*_main_pass_responses(), _repair_response(1)]
    )
    designer = _make_designer(tmp_project_dir, model, probe)

    bundle = asyncio.run(
        designer.run(
            node_id=NODE_ID, requirement_data={"name": "注册", "description": "注册功能"}
        )
    )

    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        "REQ-1-UI-RegisterPage"
    ]
    # Initial smoke + post-repair re-check.
    assert len(probe.calls) == 2
    # Main pass (write tool call + structured response), then the repair ran
    # as a third turn of the same session carrying the backend's own process
    # output back into the conversation.
    assert model.call_count == 3
    repair_text = _call_text(model.calls[2])
    assert "Boot Check Failure" in repair_text
    assert "argument handler must be a function" in repair_text


def test_boot_smoke_raises_after_repair_budget(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A still-unbootable workspace fails the stage locally with full evidence."""

    _seed_requirement(arc_runtime)
    _seed_backend(tmp_project_dir)
    probe = ScriptedProbe([BOOT_FAILURE] * (DESIGN_BOOT_SMOKE_REPAIR_ROUNDS + 1))
    model = FauxChatModel(
        responses=[
            *_main_pass_responses(),
            *[_repair_response(index) for index in range(DESIGN_BOOT_SMOKE_REPAIR_ROUNDS)],
        ]
    )
    designer = _make_designer(tmp_project_dir, model, probe)

    with pytest.raises(DesignBootSmokeError) as excinfo:
        asyncio.run(
            designer.run(
                node_id=NODE_ID,
                requirement_data={"name": "注册", "description": "注册功能"},
            )
        )

    message = str(excinfo.value)
    assert (
        f"does not boot after {DESIGN_BOOT_SMOKE_REPAIR_ROUNDS} boot repair round(s)"
        in message
    )
    assert "argument handler must be a function" in message
    assert len(probe.calls) == DESIGN_BOOT_SMOKE_REPAIR_ROUNDS + 1
    # The stage-task executor maps the failure through this category instead
    # of the generic worktree-infra one.
    assert excinfo.value.category == "design_boot_smoke"


def test_healthy_workspace_skips_repair(tmp_project_dir: Path, arc_runtime) -> None:
    """A bootable pass spends no repair asks."""

    _seed_requirement(arc_runtime)
    _seed_backend(tmp_project_dir)
    probe = ScriptedProbe([None])
    model = FauxChatModel(responses=_main_pass_responses())
    designer = _make_designer(tmp_project_dir, model, probe)

    bundle = asyncio.run(
        designer.run(
            node_id=NODE_ID, requirement_data={"name": "注册", "description": "注册功能"}
        )
    )

    assert bundle["interfaces"]
    assert len(probe.calls) == 1
    # Write tool call + structured response; no repair turn.
    assert model.call_count == 2


def test_disabled_by_env_never_probes(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ARC_DESIGN_BOOT_SMOKE=0`` turns the smoke off entirely."""

    monkeypatch.setenv("ARC_DESIGN_BOOT_SMOKE", "0")
    _seed_requirement(arc_runtime)
    _seed_backend(tmp_project_dir)
    model = FauxChatModel(responses=_main_pass_responses())
    designer = _make_designer(tmp_project_dir, model, FailingProbe())

    bundle = asyncio.run(
        designer.run(
            node_id=NODE_ID, requirement_data={"name": "注册", "description": "注册功能"}
        )
    )

    assert bundle["interfaces"]


def test_backendless_workspace_never_probes(tmp_project_dir: Path, arc_runtime) -> None:
    """A workspace without backend/package.json has nothing to boot."""

    _seed_requirement(arc_runtime)
    model = FauxChatModel(responses=_main_pass_responses())
    designer = _make_designer(tmp_project_dir, model, FailingProbe())

    bundle = asyncio.run(
        designer.run(
            node_id=NODE_ID, requirement_data={"name": "注册", "description": "注册功能"}
        )
    )

    assert bundle["interfaces"]


def test_writeless_pass_never_probes(tmp_project_dir: Path, arc_runtime) -> None:
    """A pass with no write evidence leaves the already-verified base tree."""

    _seed_requirement(arc_runtime)
    _seed_backend(tmp_project_dir)
    model = FauxChatModel(responses=_main_pass_responses(with_writes=False))
    designer = _make_designer(tmp_project_dir, model, FailingProbe())

    bundle = asyncio.run(
        designer.run(
            node_id=NODE_ID, requirement_data={"name": "注册", "description": "注册功能"}
        )
    )

    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        "REQ-1-UI-RegisterPage"
    ]

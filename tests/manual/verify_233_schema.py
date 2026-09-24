"""Manual one-shot verification for issue #233 (NOT collected by pytest).

The tightened DESIGN response schema (``InterfaceContractRecord``: required
``interface_id`` + ``type: Literal``) must decode on the REAL endpoint the
platform uses (glm-5.3-flash via the arc-bench gateway) — faux coverage alone
cannot prove provider decode compatibility. This script runs the exact
main-pass surface (langchain ``create_agent`` with the plain pydantic
response format, the same AutoStrategy resolution the stage agents hit) plus
the loose legacy schema as the 对照 arm, and reports the chosen strategy.

Manual/integration verification per AGENTS.md: spends real provider tokens
(2 small chat calls + one capability probe). Run by hand with the repo venv
from the repo root (needs .env credentials; ARC_ENV_FILE overrides the path):

    python tests/manual/verify_233_schema.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Repo-root importability regardless of the caller's cwd (pytest does this via
# pythonpath; a hand-run script does it explicitly).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import load_project_env

_CUSTOM_ENV = os.environ.get("ARC_ENV_FILE", "").strip()
load_project_env(_CUSTOM_ENV if _CUSTOM_ENV else "tests/manual/.env")
if not os.environ.get("OPENAI_API_KEY"):
    # Repo checkout convenience: the shared checkout's .env may live in the
    # main workspace when this runs from a task worktree (untracked files do
    # not propagate across worktrees).
    load_project_env(os.path.join("..", "arc-agent", ".env"))

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from agents.interface_designer import InterfaceDesignResponse
from agents.model.factory import create_arc_chat_model
from agents.model.openai_api_adapter import json_schema_structured_output_supported

PROMPT = (
    "Design the contract records for a small login feature and return them as the "
    "structured response. Exactly two records:\n"
    "1. a UI login page component at frontend/src/pages/LoginPage.tsx;\n"
    "2. a DB users table at backend/src/database/init_db.js.\n"
    "Every record MUST carry `interface_id` (include the node id REQ-9) and `type`. "
    "Keep responsibility/specification under 80 characters. You may add any extra "
    "fields you find useful (for example inputs/outputs). Do not write any files."
)


class LegacyDesignResponse(BaseModel):
    """The pre-#233 loose schema (对照 arm)."""

    summary: str = Field(default="", description="Short design-stage summary.")
    interfaces: list[dict[str, Any]] = Field(default_factory=list, description="Interface contracts.")
    files_written: list[str] = Field(default_factory=list)


def _which_strategy(model: object) -> str:
    try:
        from langchain.agents.factory import _supports_provider_strategy

        return (
            "ProviderStrategy (native json_schema strict)"
            if _supports_provider_strategy(model, tools=[])
            else "ToolStrategy (tool-calling decode)"
        )
    except Exception as exc:  # pragma: no cover - diagnostic only
        return f"unknown ({type(exc).__name__}: {exc})"


def run_case(label: str, schema: type[BaseModel]) -> bool:
    model = create_arc_chat_model(os.environ["MODEL"])
    agent = create_agent(model, tools=[], response_format=ToolStrategy(schema=schema))
    result = agent.invoke({"messages": [("user", PROMPT)]})
    structured = result.get("structured_response")
    if structured is None:
        print(f"[{label}] FAIL: no structured_response decoded")
        return False
    dump = structured.model_dump() if hasattr(structured, "model_dump") else structured
    rows = dump.get("interfaces") or []
    print(f"[{label}] decoded {len(rows)} record(s)")
    ok = bool(rows)
    for row in rows:
        record_type = str(row.get("type", ""))
        status = "OK" if record_type in {"UI", "API", "FUNC", "DB"} else "MISSING-TYPE"
        if status != "OK":
            ok = False
        print(
            f"  - {row.get('interface_id')!r:28} type={record_type!r:8} [{status}] "
            f"file={row.get('file_path', '')!r} extra_keys={sorted(set(row) - {
                'interface_id', 'type', 'req_id', 'name', 'file_path', 'first_line',
                'responsibility', 'specification', 'relation', 'callers', 'callees',
                'inputs', 'outputs', 'test_focus',
            })}"
        )
    return ok


def main() -> int:
    model_name = os.environ.get("MODEL", "")
    print(f"endpoint model: {model_name!r}")
    print(f"adapter json_schema probe: {json_schema_structured_output_supported(model_name)}")
    chat_model = create_arc_chat_model(model_name)
    print(f"langchain strategy for main pass: {_which_strategy(chat_model)}")

    results = {}
    # 对照 arm first: the loose schema the platform runs today.
    results["loose-legacy"] = run_case("loose-legacy (对照)", LegacyDesignResponse)
    # The tightened schema under test.
    results["tightened-233"] = run_case("tightened-233", InterfaceDesignResponse)

    print("\n=== verdict ===")
    for name, ok in results.items():
        print(f"{name}: {'DECODE OK' if ok else 'DECODE FAILED'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Tests for DESIGN-stage traceability overlays."""

from __future__ import annotations

import asyncio
import json

from agents.tools.traceability import build_traceability_tools


def test_traceability_tools_expose_current_design_interfaces(arc_runtime) -> None:
    tools = build_traceability_tools(
        node_id="REQ-X",
        current_interfaces=[
            {
                "interface_id": "REQ-X-FUNC-CALC",
                "req_id": "REQ-X",
                "type": "FUNC",
                "name": "Calculator",
                "file_path": "backend/src/services/calc.js",
                "first_line": "function add(a, b)",
                "responsibility": "Adds two numbers.",
                "specification": "Returns a + b.",
            }
        ],
    )

    by_requirement = json.loads(asyncio.run(tools[0]("REQ-X")))
    by_id = json.loads(asyncio.run(tools[1]("REQ-X-FUNC-CALC")))
    by_search = json.loads(asyncio.run(tools[2]("calculator", req_id="REQ-X")))

    assert by_requirement["count"] == 1
    assert by_requirement["interfaces"][0]["interface_id"] == "REQ-X-FUNC-CALC"
    assert by_id["interfaces"][0]["file_path"] == "backend/src/services/calc.js"
    assert by_search["count"] == 1

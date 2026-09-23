"""Structural guard: runner-event JSONL writes funnel through EventClient.

Issue #163 collapsed every runtime runner-event write into ``EventClient``'s
public interface. The on-disk JSONL stream (``.arc/runner-events.jsonl``) is
an ARC-Bench public contract, so a caller that appends to it directly
bypasses the schema/timestamp normalization the interface owns. This AST
check walks the production packages (``arcbench_agent_runtime``, ``core``,
``agents``, ``app_type_handler``) and the root entry scripts, and fails when
a module other than the audit module (``arcbench_agent_runtime.events``) or
its persistence helper (``arcbench_agent_runtime.jsonio``) calls
``append_jsonl`` on a runner-events path.

``core.evals`` is exempt by design: its ``runs.jsonl`` is an A/B-eval
artifact, not the runner-event stream, and it already routes its writes
through ``jsonio`` explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import agents
import app_type_handler
import arcbench_agent_runtime
import core

# Modules allowed to call append_jsonl: the audit module itself and the
# shared persistence helper it (and only it, for runner events) goes through.
_EXEMPT_MODULES = {"events.py", "jsonio.py"}
_EXEMPT_PACKAGES = {"evals.py"}
_ROOT_ENTRY_SCRIPTS = ("arc_main.py", "main.py")


def _production_source_files() -> list[Path]:
    files: list[Path] = []
    for package in (arcbench_agent_runtime, core, agents, app_type_handler):
        for root in (Path(entry) for entry in package.__path__):
            files.extend(
                path for path in root.rglob("*.py") if "__pycache__" not in path.parts
            )
    repo_root = Path(arcbench_agent_runtime.__file__).resolve().parent.parent
    files.extend(repo_root / name for name in _ROOT_ENTRY_SCRIPTS)
    return sorted(files)


def _direct_jsonl_writes(tree: ast.Module) -> list[tuple[str, int]]:
    """Functions that build/append JSONL payloads without going through EventClient.

    A runner-event write has two halves: the ``append_jsonl`` call and the
    ``runner_events_path`` it targets. Only the audit module may pair them.
    """
    offenders: list[tuple[str, int]] = []
    for function_node in ast.walk(tree):
        if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        appends = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "append_jsonl"
            for node in ast.walk(function_node)
        )
        if not appends:
            continue
        targets_runner_events = any(
            isinstance(node, ast.Attribute) and node.attr == "runner_events_path"
            for node in ast.walk(function_node)
        )
        if targets_runner_events:
            offenders.append((function_node.name, function_node.lineno))
    return offenders


def test_runner_event_jsonl_writes_live_only_in_the_audit_module() -> None:
    offenders: list[str] = []
    for source_path in _production_source_files():
        if source_path.name in _EXEMPT_MODULES:
            continue
        if source_path.parent.name == "core" and source_path.name in _EXEMPT_PACKAGES:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for function_name, lineno in _direct_jsonl_writes(tree):
            offenders.append(f"{source_path.name}:{function_name} (line {lineno})")

    assert not offenders, (
        "these functions append to runner-events JSONL directly; runner-event "
        "writes must funnel through EventClient's public interface so schema "
        "and timestamp normalization stays in one place: " + ", ".join(offenders)
    )

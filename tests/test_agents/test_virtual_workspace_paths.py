"""Faux-harness coverage for agent virtual workspace path handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.model.usage_capture import llm_usage_context
from agents.runtime.tool_usage import ToolUsageMiddleware, set_tool_usage_sink
from agents.runtime.virtual_paths import (
    PATH_CLASS_HOST_ABSOLUTE,
    PATH_CLASS_MISSING_PREFIX,
    PATH_CLASS_OTHER_WORKSPACE,
    PATH_CLASS_TRAVERSAL,
    VirtualWorkspacePathMiddleware,
    prepare_virtual_path_request,
    project_roots_for_workspace,
)
from tests.helpers.faux import drive_scripted_tool_call


def _request(name: str, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": "path-call"},
        tool=None,
        state={},
        runtime=None,
    )


def test_missing_workspace_prefix_is_normalized_by_the_faux_harness(
    tmp_project_dir: Path,
) -> None:
    (tmp_project_dir / "frontend" / "src").mkdir(parents=True)

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/frontend/src/App.tsx", "content": "export default 1;\n"},
    )

    assert content.startswith("Updated file /workspace/frontend/src/App.tsx")
    assert (tmp_project_dir / "frontend/src/App.tsx").read_text(encoding="utf-8") == "export default 1;\n"


def test_normal_workspace_path_stays_normal_in_the_faux_harness(
    tmp_project_dir: Path,
) -> None:
    target = tmp_project_dir / "backend" / "src" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("module.exports = 1;\n", encoding="utf-8")

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "/workspace/backend/src/app.js"},
    )

    assert "module.exports = 1;" in content
    assert "path diagnostic" not in content


def test_existing_cli_and_android_roots_are_discovered_safely(tmp_project_dir: Path) -> None:
    (tmp_project_dir / "app").mkdir()
    (tmp_project_dir / "tests").mkdir()
    roots = project_roots_for_workspace(tmp_project_dir)

    app_request, app_audit = prepare_virtual_path_request(
        _request("read_file", {"file_path": "/app/main.py"}),
        project_roots=roots,
    )
    tests_request, tests_audit = prepare_virtual_path_request(
        _request("glob", {"path": "/tests", "pattern": "**/*.py"}),
        project_roots=roots,
    )

    assert "app" in roots and "tests" in roots
    assert app_audit is not None and app_audit.classification == PATH_CLASS_MISSING_PREFIX
    assert app_request.tool_call["args"]["file_path"] == "/workspace/app/main.py"
    assert tests_audit is not None and tests_audit.classification == PATH_CLASS_MISSING_PREFIX
    assert tests_request.tool_call["args"]["path"] == "/workspace/tests"


@pytest.mark.parametrize(
    ("tool", "argument", "extra"),
    [
        ("read_file", "file_path", {}),
        ("write_file", "file_path", {"content": "x\n"}),
        ("edit_file", "file_path", {"old_string": "x", "new_string": "y"}),
        ("delete", "file_path", {}),
        ("append_file", "file_path", {"content": "x\n"}),
        ("ls", "path", {}),
        ("glob", "path", {"pattern": "**/*.tsx"}),
        ("grep", "path", {"pattern": "export"}),
    ],
)
def test_known_project_roots_are_normalized_for_all_path_tools(
    tool: str,
    argument: str,
    extra: dict[str, Any],
) -> None:
    args = {argument: "/backend/src/app.js", **extra}
    request, audit = prepare_virtual_path_request(_request(tool, args))

    assert audit is not None
    assert audit.classification == PATH_CLASS_MISSING_PREFIX
    assert audit.requested_path == "/backend/src/app.js"
    assert audit.execution_path == "/workspace/backend/src/app.js"
    assert request.tool_call["args"][argument] == "/workspace/backend/src/app.js"


@pytest.mark.parametrize(
    ("raw_path", "classification"),
    [
        (r"C:\Users\agent\frontend\src\App.tsx", PATH_CLASS_HOST_ABSOLUTE),
        ("/workspace/../outside.txt", PATH_CLASS_TRAVERSAL),
        ("/workspace-2/frontend/src/App.tsx", PATH_CLASS_OTHER_WORKSPACE),
    ],
)
def test_unsafe_paths_are_not_rewritten_and_receive_structured_rejection(
    raw_path: str,
    classification: str,
) -> None:
    middleware = VirtualWorkspacePathMiddleware()
    seen: dict[str, str] = {}

    def handler(request: ToolCallRequest) -> ToolMessage:
        seen["path"] = request.tool_call["args"]["file_path"]
        return ToolMessage(
            content="Error: path rejected",
            name="read_file",
            tool_call_id="path-call",
            status="error",
        )

    result = middleware.wrap_tool_call(
        _request("read_file", {"file_path": raw_path}),
        handler,
    )

    assert seen["path"] == raw_path
    assert isinstance(result, ToolMessage)
    assert f"classification: {classification}" in result.content
    assert "execution_path: <rejected>" in result.content


@pytest.mark.parametrize(
    ("raw_path", "classification"),
    [
        (r"C:\Users\agent\frontend\src\App.tsx", PATH_CLASS_HOST_ABSOLUTE),
        ("/workspace/../outside.txt", PATH_CLASS_TRAVERSAL),
        ("/workspace-2/frontend/src/App.tsx", PATH_CLASS_OTHER_WORKSPACE),
    ],
)
def test_unsafe_paths_are_rejected_by_the_faux_harness(
    tmp_project_dir: Path,
    raw_path: str,
    classification: str,
) -> None:
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": raw_path},
    )

    assert "[ARC path diagnostic]" in content
    assert f"classification: {classification}" in content


def test_path_audit_reaches_tool_usage_records() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = VirtualWorkspacePathMiddleware()
    usage = ToolUsageMiddleware()

    def handler(request: ToolCallRequest) -> ToolMessage:
        assert request.tool_call["args"]["file_path"] == "/workspace/frontend/src/App.tsx"
        return ToolMessage(
            content="ok",
            name="read_file",
            tool_call_id="path-call",
            status="success",
        )

    try:
        with llm_usage_context("REQ-PATH", "IMPLEMENT"):
            middleware.wrap_tool_call(
                _request("read_file", {"file_path": "/frontend/src/App.tsx"}),
                lambda request: usage.wrap_tool_call(request, handler),
            )
    finally:
        set_tool_usage_sink(None)

    record = records[0]
    assert record.path == "/frontend/src/App.tsx"
    assert record.requested_path == "/frontend/src/App.tsx"
    assert record.path_classification == PATH_CLASS_MISSING_PREFIX
    assert record.execution_path == "/workspace/frontend/src/App.tsx"

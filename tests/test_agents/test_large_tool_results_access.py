"""Evicted tool results and human messages must remain readable through file tools.

issue #305：ARC 挂载的 grep 工具描述沿用上游句子，建议模型在
`/large_tool_results/` 下搜索被 offload 的大结果；但公共 prompt 声明文件工具
只允许 `/workspace` 与 `/skills/<name>/SKILL.md`，权限层末尾的 `/**` deny 也把
该目录拒之门外——描述在推荐一条不可达的路径。而上游 `FilesystemMiddleware` 的
eviction（``tool_token_limit_before_evict`` 默认 20000 token）是真实开启的：
超限的工具结果（``run_tests`` 输出等）经 composite 默认路由落进 StateBackend
（agent state，非宿主路径），替换消息 ``Tool result too large`` 会把
`/large_tool_results/<tool_call_id>` 指给模型，模型却读不了。

修复（issue 给出的第二条路线）不新增路由、不开放任何宿主路径：为该固定虚拟
前缀补一条最小只读 allow（写仍 deny），公共 prompt 与 grep 描述同步为真实语义。
测试按验收口径同时钉住四个面：工具可见描述、公共 prompt、权限判断、明确的
offload 指针回读；并默认拒绝任意宿主路径、权限拒绝不得渲染成「搜索无匹配」。
issue #316 在同一个 StateBackend 默认路由下将过长的 human message 移到
`/conversation_history/<uuid>.md`；模型收到的指针也必须可读，且不可写。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import _check_fs_permission
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from agents.context.prompts.common import workspace_tool_policy
from agents.runtime.filesystem_adapters import (
    ARC_GREP_TOOL_DESCRIPTION,
    ARCFilesystemMiddleware,
    CONVERSATION_HISTORY_PREFIX,
    LARGE_TOOL_RESULTS_PREFIX,
    workspace_filesystem_backend,
)
from tests.helpers.faux import FauxChatModel, drive_scripted_tool_call


# -- production-shaped adapter ---------------------------------------------------


def _make_middleware(tmp_project_dir: Path) -> ARCFilesystemMiddleware:
    """Build the production adapter over ARC's real backend routes and permissions.

    Mirrors the factory's construction (same composite shape, same permission
    rules from ``factory._build_filesystem_permissions``) so permission
    semantics are observed exactly as the model experiences them.
    """

    from agents.runtime.factory import _build_filesystem_permissions

    root = tmp_project_dir.resolve()
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": workspace_filesystem_backend(str(root)),
        },
    )
    return ARCFilesystemMiddleware(
        backend=backend,
        _permissions=_production_permissions(root),
    )


def _production_permissions(root: Path) -> list[Any]:
    from agents.runtime.factory import _build_filesystem_permissions

    return _build_filesystem_permissions(
        root,
        [str(root)],
        skill_instruction_paths=[],
    )


def _tools(tmp_project_dir: Path) -> dict[str, Any]:
    return {tool.name: tool for tool in _make_middleware(tmp_project_dir).tools}


def _runtime(call_id: str = "call-large-1") -> Any:
    from langgraph.prebuilt.tool_node import ToolRuntime

    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id=call_id,
        store=None,
        tools=[],
    )


# -- tool-visible description ------------------------------------------------------


def test_grep_description_still_advertises_the_offload_root(
    tmp_project_dir: Path,
) -> None:
    middleware = _make_middleware(tmp_project_dir)
    tool = next(t for t in middleware.tools if getattr(t, "name", "") == "grep")

    # The mounted description is ARC's override and still points at the
    # offload root — but that suggestion is now backed by the permission
    # grant below, so it describes a reachable path.
    assert tool.description == ARC_GREP_TOOL_DESCRIPTION
    assert LARGE_TOOL_RESULTS_PREFIX in tool.description
    assert "by default" not in tool.description


def test_offload_prefix_constant_matches_the_middleware_reality(
    tmp_project_dir: Path,
) -> None:
    middleware = _make_middleware(tmp_project_dir)

    # The constant the permissions and prompt are written against must equal
    # the prefix upstream's eviction actually writes to (CompositeBackend's
    # default artifacts root is "/", so the prefix is fixed, not configurable).
    assert middleware._large_tool_results_prefix == LARGE_TOOL_RESULTS_PREFIX
    assert middleware._conversation_history_prefix == CONVERSATION_HISTORY_PREFIX


# -- common prompt policy ----------------------------------------------------------


def test_tool_policy_names_the_offload_exception() -> None:
    policy = workspace_tool_policy()

    # Direct skill reads and both eviction roots are explicit exceptions.
    assert "The sole exception" not in policy
    assert "/skills/<skill-name>/SKILL.md" in policy
    assert LARGE_TOOL_RESULTS_PREFIX in policy
    assert CONVERSATION_HISTORY_PREFIX in policy
    # Both eviction roots are stated as read-only, following their pointers.
    assert "Tool result too large" in policy
    assert "Message content too large" in policy


# -- permission judgments ----------------------------------------------------------


def test_permission_matrix_allows_only_reads_of_the_offload_roots(
    tmp_project_dir: Path,
) -> None:
    permissions = _production_permissions(tmp_project_dir.resolve())

    def verdict(operation: str, path: str) -> str:
        return _check_fs_permission(permissions, operation, path)

    # The offload root is readable (including nested offloaded files) …
    assert verdict("read", f"{LARGE_TOOL_RESULTS_PREFIX}/call-1") == "allow"
    assert verdict("read", f"{LARGE_TOOL_RESULTS_PREFIX}/call-1/deep") == "allow"
    # … but never writable by the model, and the rest of the virtual root
    # outside /workspace stays denied. (Drive-letter host paths are refused
    # earlier, by path validation — pinned at tool level below; the pattern
    # matcher only ever sees virtual paths.)
    assert verdict("write", f"{LARGE_TOOL_RESULTS_PREFIX}/call-1") == "deny"
    assert verdict("read", f"{CONVERSATION_HISTORY_PREFIX}/message.md") == "allow"
    assert verdict("read", f"{CONVERSATION_HISTORY_PREFIX}/nested/message.md") == "allow"
    assert verdict("write", f"{CONVERSATION_HISTORY_PREFIX}/message.md") == "deny"
    assert verdict("read", "/conversation_history_other/message.md") == "deny"
    assert verdict("read", "/etc/passwd") == "deny"
    # The pre-existing grant surface is unchanged.
    assert verdict("read", "/workspace/src/a.py") == "allow"
    assert verdict("write", "/workspace/src/a.py") == "allow"


def test_windows_host_paths_are_refused_by_path_validation(
    tmp_project_dir: Path,
) -> None:
    """The host-path defense for drive-letter forms sits in path validation,
    upstream of the permission list; the offload grant must not have moved it."""

    tool = _tools(tmp_project_dir)["read_file"]
    message = tool.func(
        file_path="C:/Users/someone/.env",
        runtime=_runtime(),
    )

    content = str(message.content)
    assert "Windows absolute paths are not supported" in content


def test_read_of_missing_offloaded_file_is_not_found_not_denied(
    tmp_project_dir: Path,
) -> None:
    """The read grant must open the route: a missing file reports the backend's
    not-found answer, never a permission denial."""

    with _state_files_store():
        tool = _tools(tmp_project_dir)["read_file"]
        message = tool.func(
            file_path=f"{LARGE_TOOL_RESULTS_PREFIX}/never-offloaded",
            runtime=_runtime(),
        )

    content = str(message.content)
    assert "permission denied" not in content
    assert "not found" in content


def test_write_into_the_offload_root_stays_denied(tmp_project_dir: Path) -> None:
    tool = _tools(tmp_project_dir)["write_file"]
    message = tool.func(
        file_path=f"{LARGE_TOOL_RESULTS_PREFIX}/model-written",
        content="nope",
        runtime=_runtime(),
    )

    assert "permission denied" in str(message.content)


# -- explicit offload pointer round-trip --------------------------------------------


def test_evicted_tool_result_pointer_is_readable_end_to_end(
    tmp_project_dir: Path,
) -> None:
    """The full loop the grep description and pointer promise: an oversized
    tool result is evicted to `/large_tool_results/<tool_call_id>`, the
    replacement message hands the model that path, and `read_file` on it
    returns the full content."""

    middleware = _make_middleware(tmp_project_dir)
    marker = "OFFLOADED-MARKER-7f3a9c"
    huge_output = "\n".join(f"test row {i} {marker}" for i in range(3000))
    assert len(huge_output) > 80_000  # upstream eviction threshold: 4 chars/token * 20000

    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langchain_core.messages import ToolMessage

    request = ToolCallRequest(
        tool_call={"name": "run_tests", "args": {}, "id": "call-big-1"},
        tool=None,
        state={},
        runtime=None,
    )
    handler_result = ToolMessage(
        content=huge_output, tool_call_id="call-big-1", name="run_tests"
    )

    with _state_files_store() as files:
        result = middleware.wrap_tool_call(request, lambda _request: handler_result)

        content = str(result.content)
        assert "Tool result too large" in content
        pointer_match = re.search(rf"{LARGE_TOOL_RESULTS_PREFIX}/\S+", content)
        assert pointer_match, content
        pointer_path = pointer_match.group(0)

        # The pointer path the model receives is readable through the real
        # read_file tool over the same backend, and returns the full content.
        read_tool = next(t for t in middleware.tools if getattr(t, "name", "") == "read_file")
        message = read_tool.func(
            file_path=pointer_path,
            runtime=_runtime("call-big-1"),
            offset=0,
            limit=2000,
        )
        assert marker in str(message.content)
        # The offload write landed in the agent-state store, not on disk.
        assert pointer_path in files

        # The description's other promise also holds over real evicted
        # content: grepping the directory finds the offloaded result the
        # pointer's exact path was unknown for.
        grep_tool = next(t for t in middleware.tools if getattr(t, "name", "") == "grep")
        grep_message = grep_tool.func(
            pattern=marker,
            runtime=_runtime("call-grep-1"),
            path=LARGE_TOOL_RESULTS_PREFIX,
        )
        assert pointer_path in str(grep_message.content)
        assert "permission denied" not in str(grep_message.content)


# -- human-message eviction pointer round-trip ----------------------------------------


def test_evicted_human_message_pointer_is_readable_end_to_end(
    tmp_project_dir: Path,
) -> None:
    middleware = _make_middleware(tmp_project_dir)
    marker = "HUMAN-HISTORY-MARKER-316"
    content = "\n".join(f"history row {i} {marker}" for i in range(7000))
    assert len(content) > 200_000  # upstream default human-message threshold
    request = ModelRequest(
        model=FauxChatModel(), messages=[HumanMessage(content=content, id="human-316")]
    )
    model_messages: list[Any] = []

    def handler(model_request: ModelRequest[Any]) -> ModelResponse[Any]:
        model_messages.extend(model_request.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    with _state_files_store() as files:
        middleware.wrap_model_call(request, handler)
        assert model_messages
        pointer_match = re.search(
            r"/conversation_history/[a-f0-9-]+\.md", str(model_messages[0].content)
        )
        assert pointer_match, model_messages[0].content
        pointer_path = pointer_match.group(0)
        assert pointer_path in files

        read_tool = next(tool for tool in middleware.tools if tool.name == "read_file")
        message = read_tool.func(
            file_path=pointer_path, runtime=_runtime("call-human-316"), offset=6999, limit=1
        )
        assert f"history row 6999 {marker}" in str(message.content)
        assert "permission denied" not in str(message.content)


# -- build-path nails -----------------------------------------------------------------


def test_build_path_grep_on_the_offload_root_is_allowed_and_can_miss(
    tmp_project_dir: Path,
) -> None:
    """A grep aimed at the offload root runs (allowed) and a miss renders as a
    genuine no-match — never as a permission denial dressed up as one."""

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "grep",
        {"pattern": "anything", "path": LARGE_TOOL_RESULTS_PREFIX},
    )

    assert content.startswith("No matches found")
    assert "permission denied" not in content


def test_build_path_denied_scope_is_an_error_not_a_no_match(
    tmp_project_dir: Path,
) -> None:
    """The mirror guarantee: a scope the policy denies (listing under /skills)
    comes back as a permission error with error status — not as `No matches
    found` (issue #305 acceptance: denial must not read as search-no-match)."""

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "grep",
        {"pattern": "anything", "path": "/skills"},
    )

    assert content.startswith("Error: permission denied")
    assert "No matches found" not in content


def test_build_path_read_of_offload_root_is_allowed_but_missing(
    tmp_project_dir: Path,
) -> None:
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": f"{LARGE_TOOL_RESULTS_PREFIX}/call-probe-1"},
    )

    assert "permission denied" not in content
    assert "not found" in content


def test_build_path_read_of_missing_human_history_is_not_denied(
    tmp_project_dir: Path,
) -> None:
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": f"{CONVERSATION_HISTORY_PREFIX}/never-offloaded.md"},
    )
    assert "permission denied" not in content
    assert "not found" in content


def test_write_into_human_history_stays_denied(tmp_project_dir: Path) -> None:
    tool = _tools(tmp_project_dir)["write_file"]
    message = tool.func(
        file_path=f"{CONVERSATION_HISTORY_PREFIX}/model-written.md",
        content="nope",
        runtime=_runtime(),
    )
    assert "permission denied" in str(message.content)


# -- simulated graph-state channel ------------------------------------------------------


@contextmanager
def _state_files_store() -> Any:
    """Stand in for LangGraph's `files` channel behind ``StateBackend``.

    ``StateBackend`` reads and writes the channel through the
    ``CONFIG_KEY_READ``/``CONFIG_KEY_SEND`` entries of the ambient runnable
    config; providing them lets adapter-level tests exercise the real backend
    write/read path (eviction offload, pointer read-back) without a full
    graph, exactly as a node execution would.
    """

    from langchain_core.runnables.config import var_child_runnable_config
    from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SEND

    files: dict[str, Any] = {}

    def _read(channel: str, fresh: bool = False) -> dict[str, Any]:
        assert channel == "files"
        return dict(files)

    def _send(writes: list[tuple[str, Any]]) -> None:
        for channel, update in writes:
            if channel == "files":
                files.update(update)

    config = {"configurable": {CONFIG_KEY_READ: _read, CONFIG_KEY_SEND: _send}}
    token = var_child_runnable_config.set(config)
    try:
        yield files
    finally:
        var_child_runnable_config.reset(token)

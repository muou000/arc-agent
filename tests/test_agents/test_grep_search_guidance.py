"""grep 结果注释必须与机械现实一致，且同一 path 的无命中循环要有预算。

arc-output-serial-4 的 REQ-1 实测：模型反复用 `a|b|c` 形态的 pattern 发 grep ——
上游按字面量匹配，必返 "No matches found"，结果注释还建议 "run a separate search
per alternative"，随后模型退化为单字面量逐个试（`用户名`、`Found`、`Unable`、
`href`、`3B99FC`……）。88 次 grep 中约半数是 alternation 误用，每次重试都是一次
完整 LLM 调用（实测单调用上下文 15.6 万 token）。

issue #218 的两个互补修复，均落在 deepagents 已暴露的缝上（构建路径不改上游类或
模块属性，与 ``filesystem_adapters`` 的其余适配器同一模式）：

- ``ArcCompositeBackend`` 在 composite 缝上把含 `|` 的 pattern 展开成字面量分支，
  逐支走上游 grep 引擎，再把结构化匹配按 (path, line) 去重合并 —— 三种 output_mode、
  权限过滤、max_count 截断注记全部保持上游行为；
- ``GrepGuidanceMiddleware`` 在工具边界把上游 regex 提示（其"每个候选单独搜一次"
  的措辞会助推逐字面量循环）替换为与展开语义一致的 ARC 注释，并在同一 path 连续
  无命中达到预算时注入换策略提示。

测试分两层：直接驱动 ``build_stage_agent`` 的 build-path 探针断言"模型实际收到的
文本"，单测覆盖拆分/合并/预算计数的确定性语义。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agents.context.prompts.common import workspace_tool_policy
from agents.runtime.factory import OpenAIGrepSchema
from agents.runtime.filesystem_adapters import (
    ARC_GREP_TOOL_DESCRIPTION,
    ARCFilesystemMiddleware,
    ArcCompositeBackend,
    GrepGuidanceMiddleware,
    MAX_GREP_ALTERNATIVES,
    alternation_expansion,
    split_literal_alternation,
    workspace_filesystem_backend,
)
from deepagents.backends import StateBackend
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call

# The upstream advice sentence (deepagents regex_literal_hint) that coaches the
# per-alternative call loop; no model-facing grep surface may contain it.
_BANNED_LOOP_ADVICE = "run a separate search per alternative"


# -- splitter units ------------------------------------------------------------


def test_split_returns_none_without_a_pipe() -> None:
    assert split_literal_alternation("plain literal") is None


def test_split_produces_trimmed_deduped_branches() -> None:
    assert split_literal_alternation("用户名 | Found || 用户名|") == ["用户名", "Found"]


def test_split_unescapes_a_literal_pipe() -> None:
    # `\|` is the documented escape for searching the literal `|` text.
    assert split_literal_alternation("a\\|b") == ["a|b"]
    assert split_literal_alternation("a|b\\|c") == ["a", "b|c"]


def test_expansion_reports_cap_and_total() -> None:
    pattern = "|".join(f"b{i}" for i in range(MAX_GREP_ALTERNATIVES + 3))
    expansion = alternation_expansion(pattern)
    assert expansion is not None
    assert expansion.total == MAX_GREP_ALTERNATIVES + 3
    assert len(expansion.searched) == MAX_GREP_ALTERNATIVES
    assert expansion.expanded
    assert expansion.dropped == 3


def test_expansion_is_none_for_plain_literals() -> None:
    assert alternation_expansion("plain literal") is None


# -- backend merge units ---------------------------------------------------------


def _composite(tmp_path: Path) -> ArcCompositeBackend:
    return ArcCompositeBackend(
        default=StateBackend(),
        routes={"/workspace/": workspace_filesystem_backend(str(tmp_path))},
    )


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_alternation_union_across_branches(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", "alpha\n")
    _write(tmp_path, "b.txt", "beta\n")
    _write(tmp_path, "c.txt", "alpha beta\n")

    result = _composite(tmp_path).grep("alpha|beta", path="/workspace")

    paths = {match["path"] for match in result.matches or []}
    assert paths == {"/workspace/a.txt", "/workspace/b.txt", "/workspace/c.txt"}
    # The c.txt line matches both branches; the merge dedupes by (path, line).
    c_matches = [m for m in result.matches or [] if m["path"] == "/workspace/c.txt"]
    assert len(c_matches) == 1
    assert not result.error
    assert not result.truncated


def test_alternation_miss_is_a_clean_no_match(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", "alpha\n")

    result = _composite(tmp_path).grep("zzz1|zzz2", path="/workspace")

    assert result.matches == []
    assert not result.error
    assert not result.truncated


def test_alternation_honors_max_count_with_truncation_flag(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", "\n".join(f"hit {i} alpha" for i in range(10)) + "\n")

    result = _composite(tmp_path).grep("alpha|a.txt", path="/workspace", max_count=4)

    assert len(result.matches or []) == 4
    assert result.truncated


def test_alternation_propagates_backend_errors(tmp_path: Path) -> None:
    result = _composite(tmp_path).grep("alpha|beta", path="/workspace", glob="../outside")

    assert result.error
    assert not result.matches


def test_alternation_agrep_mirrors_grep(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", "alpha\n")
    _write(tmp_path, "b.txt", "beta\n")

    result = asyncio.run(_composite(tmp_path).agrep("alpha|beta", path="/workspace"))

    assert {match["path"] for match in result.matches or []} == {
        "/workspace/a.txt",
        "/workspace/b.txt",
    }


def test_plain_pattern_passes_through_unchanged(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", "alpha\n")

    result = _composite(tmp_path).grep("alpha", path="/workspace")

    assert [match["path"] for match in result.matches or []] == ["/workspace/a.txt"]
    assert not result.truncated


# -- guidance middleware units -----------------------------------------------------


def _grep_request(pattern: str, path: str | None = "/workspace") -> Any:
    from langgraph.prebuilt.tool_node import ToolCallRequest

    return ToolCallRequest(
        tool_call={"name": "grep", "args": {"pattern": pattern, "path": path}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )


def _tool_message(content: str, *, status: str = "success") -> Any:
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id="call-1", name="grep", status=status)


def _noop_handler(message: _tool_message) -> Any:  # type: ignore[valid-type]
    return lambda request: message


def test_expansion_note_on_matching_alternation_result() -> None:
    middleware = GrepGuidanceMiddleware()
    result = middleware.wrap_tool_call(
        _grep_request("alpha|beta"), _noop_handler(_tool_message("/workspace/a.txt"))
    )

    assert result.content.startswith("/workspace/a.txt")
    assert "literal alternatives" in result.content
    assert "`alpha`" in result.content and "`beta`" in result.content
    assert _BANNED_LOOP_ADVICE not in result.content


def test_upstream_regex_note_is_replaced_on_alternation_miss() -> None:
    middleware = GrepGuidanceMiddleware()
    raw = "No matches found\n\n" + (
        "Note: grep matches literal text, not regex, so characters like "
        "`|`, `.*`, and `\\.` are searched verbatim. Search for the literal "
        "text you need instead; for `|` alternation, run a separate search "
        "per alternative."
    )
    result = middleware.wrap_tool_call(_grep_request("alpha|beta"), _noop_handler(_tool_message(raw)))

    assert result.content.startswith("No matches found")
    assert _BANNED_LOOP_ADVICE not in result.content
    assert "`alpha`" in result.content and "`beta`" in result.content
    assert "literal text" in result.content


def test_regex_signal_miss_without_pipe_also_gets_arc_note() -> None:
    middleware = GrepGuidanceMiddleware()
    raw = "No matches found\n\n" + (
        "Note: grep matches literal text, not regex, so characters like "
        "`|`, `.*`, and `\\.` are searched verbatim. Search for the literal "
        "text you need instead; for `|` alternation, run a separate search "
        "per alternative."
    )
    result = middleware.wrap_tool_call(_grep_request("foo.*bar"), _noop_handler(_tool_message(raw)))

    assert _BANNED_LOOP_ADVICE not in result.content
    assert "literal text" in result.content


def test_error_results_pass_through_untouched() -> None:
    middleware = GrepGuidanceMiddleware()
    raw = "Error: permission denied for read on /workspace/secret"
    result = middleware.wrap_tool_call(
        _grep_request("alpha|beta"), _noop_handler(_tool_message(raw, status="error"))
    )

    assert result.content == raw


def test_non_grep_tools_pass_through_untouched() -> None:
    middleware = GrepGuidanceMiddleware()
    from langgraph.prebuilt.tool_node import ToolCallRequest

    request = ToolCallRequest(
        tool_call={"name": "read_file", "args": {"file_path": "/workspace/x"}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )
    result = middleware.wrap_tool_call(request, _noop_handler(_tool_message("file body")))
    assert result.content == "file body"


# -- no-match budget --------------------------------------------------------------


def _miss(pattern: str = "zzz") -> Any:
    return _noop_handler(_tool_message("No matches found"))


def test_budget_hint_after_consecutive_misses_on_same_scope() -> None:
    middleware = GrepGuidanceMiddleware()
    for expected_count in range(1, 4):
        result = middleware.wrap_tool_call(_grep_request("zzz"), _miss("zzz"))
        if expected_count < 3:
            assert "No-match budget" not in result.content
        else:
            assert f"{expected_count} consecutive no-match greps on /workspace" in result.content


def test_budget_tiers_escalate() -> None:
    middleware = GrepGuidanceMiddleware()
    sixth = None
    for count in range(1, 7):
        sixth = middleware.wrap_tool_call(_grep_request("zzz"), _miss("zzz"))
    assert sixth is not None
    assert "6 consecutive no-match greps" in sixth.content
    assert "very likely absent" in sixth.content


def test_matching_grep_resets_the_streak() -> None:
    middleware = GrepGuidanceMiddleware()
    for _ in range(2):
        middleware.wrap_tool_call(_grep_request("zzz"), _miss("zzz"))
    middleware.wrap_tool_call(_grep_request("alpha"), _noop_handler(_tool_message("/workspace/a.txt")))
    result = middleware.wrap_tool_call(_grep_request("zzz"), _miss("zzz"))

    assert "No-match budget" not in result.content


def test_streaks_are_tracked_per_scope() -> None:
    middleware = GrepGuidanceMiddleware()
    for _ in range(2):
        middleware.wrap_tool_call(_grep_request("zzz", path="/workspace/backend"), _miss("zzz"))
    result = middleware.wrap_tool_call(_grep_request("zzz", path="/workspace/frontend"), _miss("zzz"))

    assert "No-match budget" not in result.content
    backend_hit = middleware.wrap_tool_call(_grep_request("zzz", path="/workspace/backend"), _miss("zzz"))
    assert "3 consecutive no-match greps on /workspace/backend" in backend_hit.content


def test_errors_do_not_count_toward_the_budget() -> None:
    middleware = GrepGuidanceMiddleware()
    for _ in range(3):
        middleware.wrap_tool_call(
            _grep_request("zzz"),
            _noop_handler(_tool_message("Error: permission denied for read on /workspace", status="error")),
        )
    result = middleware.wrap_tool_call(_grep_request("zzz"), _miss("zzz"))
    assert "No-match budget" not in result.content


# -- build-path nails --------------------------------------------------------------


def _drive_scripted_greps(
    workspace_root: Path,
    grep_args_list: list[dict[str, Any]],
    *,
    stage: str = "implementation",
) -> list[str]:
    """Drive scripted grep calls through one real ``build_stage_agent`` agent.

    Same harness as ``tests.helpers.faux.drive_scripted_tool_call`` but with
    one scripted grep per assistant turn, so consecutive-grep state (the
    no-match budget) is observed the way a real retry loop produces it —
    across turns, not inside one parallel batch (parallel tool calls resume
    concurrently, so within a turn exactly one result carries the budget hint
    once the shared streak crosses the threshold, but which one is racy).
    """

    from agents.runtime.contracts import AgentRuntimeContext
    from agents.runtime.factory import build_stage_agent
    from agents.runtime.runners import ainvoke_stage_agent

    phase = {"implementation": "IMPLEMENT", "test_generation": "TEST_GENERATION"}.get(stage)
    if phase is None:
        raise ValueError(f"unsupported probe stage: {stage!r}")

    responses: list[Any] = [
        faux_tool_call("grep", args, call_id=f"grep-call-{index}")
        for index, args in enumerate(grep_args_list)
    ]
    model = FauxChatModel(responses=[*responses, faux_text("DONE")])
    built = build_stage_agent(
        name="grep_guidance_probe",
        stage=stage,
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(workspace_root),
        writable_roots=[str(workspace_root)],
        skills=[],
        memory=[],
        tools=[],
        checkpointer=None,
    )
    asyncio.run(
        ainvoke_stage_agent(
            built.agent,
            message="run the scripted grep calls",
            context=AgentRuntimeContext(
                node_id="REQ-GREP-PROBE",
                phase=phase,
                app_type="web",
                workspace_root=str(workspace_root),
                requirement_path="",
            ),
            thread_id="REQ-GREP-PROBE:grep",
            label="GrepGuidanceProbe",
        )
    )
    by_call_id: dict[str, str] = {}
    for turn in model.calls:
        for message in turn:
            if getattr(message, "type", "") == "tool":
                # Each model call replays the full conversation; keep the
                # latest content per tool_call_id.
                by_call_id[str(getattr(message, "tool_call_id", ""))] = str(message.content)
    contents = [by_call_id[f"grep-call-{index}"] for index in range(len(grep_args_list))]
    if len(contents) != len(grep_args_list):
        raise AssertionError(
            f"expected {len(grep_args_list)} tool result(s), got {len(contents)}"
        )
    return contents


def test_build_path_alternation_hit_returns_union_with_expansion_note(
    tmp_project_dir: Path,
) -> None:
    (tmp_project_dir / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_project_dir / "beta.txt").write_text("beta\n", encoding="utf-8")

    (content,) = _drive_scripted_greps(
        tmp_project_dir,
        [{"pattern": "alpha|beta", "path": "/workspace"}],
    )

    assert "/workspace/alpha.txt" in content
    assert "/workspace/beta.txt" in content
    assert "literal alternatives" in content
    assert _BANNED_LOOP_ADVICE not in content


def test_build_path_alternation_miss_gets_arc_note(tmp_project_dir: Path) -> None:
    (tmp_project_dir / "alpha.txt").write_text("alpha\n", encoding="utf-8")

    (content,) = _drive_scripted_greps(
        tmp_project_dir,
        [{"pattern": "zzz1|zzz2", "path": "/workspace"}],
    )

    assert content.startswith("No matches found")
    assert _BANNED_LOOP_ADVICE not in content
    assert "`zzz1`" in content and "`zzz2`" in content
    assert "literal" in content


def test_build_path_budget_hint_on_third_consecutive_miss(
    tmp_project_dir: Path,
) -> None:
    (tmp_project_dir / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    miss_args = {"pattern": "zzz", "path": "/workspace"}

    contents = _drive_scripted_greps(tmp_project_dir, [miss_args, miss_args, miss_args])

    assert all(content.startswith("No matches found") for content in contents)
    assert "No-match budget" not in contents[0]
    assert "No-match budget" not in contents[1]
    assert "3 consecutive no-match greps on /workspace" in contents[2]


# -- tool description and schema surfaces -------------------------------------------


def test_arc_grep_tool_description_matches_expansion_semantics(tmp_path: Path) -> None:
    middleware = ARCFilesystemMiddleware(
        backend=workspace_filesystem_backend(str(tmp_path)),
        _permissions=[],
    )
    tool = next(t for t in middleware.tools if getattr(t, "name", "") == "grep")

    description = tool.description
    assert "literal alternatives" in description
    assert "\\|" in description
    assert "run a separate grep for each" not in description
    assert ARC_GREP_TOOL_DESCRIPTION == description


def test_arc_grep_description_is_the_request_time_override(tmp_path: Path) -> None:
    middleware = ARCFilesystemMiddleware(
        backend=workspace_filesystem_backend(str(tmp_path)),
        _permissions=[],
    )
    # The request-time description filter must keep the ARC text (a custom
    # description short-circuits upstream's execution-visibility rewrite).
    assert middleware._grep_tool_description(include_execution=False) == ARC_GREP_TOOL_DESCRIPTION
    assert middleware._grep_tool_description(include_execution=True) == ARC_GREP_TOOL_DESCRIPTION


def test_openai_grep_schema_pattern_description_states_alternation() -> None:
    description = OpenAIGrepSchema.model_fields["pattern"].description or ""
    assert "literal alternatives" in description
    assert "not regex" in description
    assert _BANNED_LOOP_ADVICE not in description


def test_tool_policy_teaches_alternation_and_anti_loop() -> None:
    policy = workspace_tool_policy()
    lines = [line for line in policy.splitlines() if "`grep` matches literal text" in line]
    assert len(lines) == 1
    assert "literal alternatives" in lines[0]
    assert _BANNED_LOOP_ADVICE not in policy
    # The anti-loop steer must sit on the same line as the semantics.
    assert "read_file" in lines[0]

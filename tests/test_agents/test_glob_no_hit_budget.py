"""glob 的无命中循环要有与 grep 对称的预算止损（issue #298）。

2026-09-25 晚 easy-ticketbooking run 实测：REQ-1 终局前 15 分钟打了约 110 次
无效 glob（`node_modules/vitest/dist/**/cli*` 家族连打 48 次），每次都返回裸
``No files found`` 而没有任何信号提示换策略，run 就耗死在这个循环里。
grep 侧自 #218/#228 起已有同型预算（连续无命中按档位升级 nudge、命中重置、
错误不计不重置）；本票为 glob 装上对称防护，分档阈值与重置语义直接复用
grep 侧设计。

测试分两层：直接驱动 ``GlobGuidanceMiddleware`` 的单测覆盖计数与升级语义，
build-path 探针经由真实 ``build_stage_agent`` 断言模型实际收到的回执。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime.filesystem_adapters import (
    _GLOB_NO_FILES_SENTINEL,
    _GLOB_NO_HIT_ESCALATE_AFTER,
    _GLOB_NO_HIT_HINT_AFTER,
    _GREP_NO_MATCH_ESCALATE_AFTER,
    _GREP_NO_MATCH_HINT_AFTER,
    GlobGuidanceMiddleware,
)
from tests.helpers.faux import drive_scripted_tool_turns

# The upstream empty-result sentinel (deepagents `_format_file_paths([])`).
_BARE_EMPTY = "No files found"


# -- middleware units ------------------------------------------------------------


def _glob_request(path: str | None = "/workspace", pattern: str = "**/cli*") -> Any:
    from langgraph.prebuilt.tool_node import ToolCallRequest

    args: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        args["path"] = path
    return ToolCallRequest(
        tool_call={"name": "glob", "args": args, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )


def _tool_message(content: str, *, status: str = "success") -> Any:
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id="call-1", name="glob", status=status)


def _handler_returning(message: Any) -> Any:
    """A tool-call handler that always returns the given (pre-built) result."""

    return lambda request: message


def _miss() -> Any:
    return _handler_returning(_tool_message(_BARE_EMPTY))


def test_budget_hint_after_consecutive_no_hit_globs_on_same_scope() -> None:
    middleware = GlobGuidanceMiddleware()
    for expected_count in range(1, 4):
        result = middleware.wrap_tool_call(_glob_request(), _miss())
        if expected_count < _GLOB_NO_HIT_HINT_AFTER:
            assert "Glob budget" not in result.content
        else:
            assert f"{expected_count} consecutive no-hit globs on /workspace" in result.content


def test_budget_tiers_escalate() -> None:
    middleware = GlobGuidanceMiddleware()
    sixth = None
    for _ in range(_GLOB_NO_HIT_ESCALATE_AFTER):
        sixth = middleware.wrap_tool_call(_glob_request(), _miss())
    assert sixth is not None
    assert f"{_GLOB_NO_HIT_ESCALATE_AFTER} consecutive no-hit globs" in sixth.content
    assert "very likely absent" in sixth.content


def test_a_hit_resets_the_streak() -> None:
    middleware = GlobGuidanceMiddleware()
    for _ in range(2):
        middleware.wrap_tool_call(_glob_request(), _miss())
    middleware.wrap_tool_call(
        _glob_request(pattern="*.txt"),
        _handler_returning(_tool_message("/workspace/a.txt")),
    )
    result = middleware.wrap_tool_call(_glob_request(), _miss())

    assert "Glob budget" not in result.content


def test_streaks_are_tracked_per_scope() -> None:
    middleware = GlobGuidanceMiddleware()
    for _ in range(2):
        middleware.wrap_tool_call(_glob_request(path="/workspace/backend"), _miss())
    result = middleware.wrap_tool_call(_glob_request(path="/workspace/frontend"), _miss())

    assert "Glob budget" not in result.content
    backend_hit = middleware.wrap_tool_call(
        _glob_request(path="/workspace/backend"), _miss()
    )
    assert "3 consecutive no-hit globs on /workspace/backend" in backend_hit.content


def test_errors_do_not_count_toward_the_budget() -> None:
    middleware = GlobGuidanceMiddleware()
    for _ in range(3):
        middleware.wrap_tool_call(
            _glob_request(),
            _handler_returning(
                _tool_message("Error: permission denied for read on /workspace/.arc", status="error")
            ),
        )
    result = middleware.wrap_tool_call(_glob_request(), _miss())
    assert "Glob budget" not in result.content


def test_errors_do_not_reset_the_streak_either() -> None:
    middleware = GlobGuidanceMiddleware()
    for _ in range(2):
        middleware.wrap_tool_call(_glob_request(), _miss())
    middleware.wrap_tool_call(
        _glob_request(),
        _handler_returning(_tool_message("Error: permission denied for read on /workspace/.arc", status="error")),
    )
    result = middleware.wrap_tool_call(_glob_request(), _miss())
    assert "3 consecutive no-hit globs" in result.content


def test_empty_result_with_trailing_note_still_counts_as_no_hit() -> None:
    # A truncated empty result (sentinel + truncation note) is still a no-hit
    # from the model's perspective; the first-paragraph sentinel check sees it.
    middleware = GlobGuidanceMiddleware()
    truncated_empty = _handler_returning(_tool_message(_BARE_EMPTY + "\n\nSome truncation note"))
    for _ in range(_GLOB_NO_HIT_HINT_AFTER):
        middleware.wrap_tool_call(_glob_request(), truncated_empty)
    result = middleware.wrap_tool_call(_glob_request(), truncated_empty)
    assert "Glob budget" in result.content


def test_non_glob_tools_pass_through_untouched() -> None:
    from langgraph.prebuilt.tool_node import ToolCallRequest

    middleware = GlobGuidanceMiddleware()
    # A grep-shaped sentinel under a glob call name is not this middleware's
    # business (the grep guidance owns it), and non-glob names are ignored.
    request = ToolCallRequest(
        tool_call={"name": "read_file", "args": {"file_path": "/workspace/x"}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )
    untouched = middleware.wrap_tool_call(
        request, _handler_returning(_tool_message(_BARE_EMPTY))
    )
    assert untouched.content == _BARE_EMPTY


# -- ladder alignment with the grep side -------------------------------------------


def test_glob_ladder_matches_the_grep_budget() -> None:
    # Issue #298: thresholds and reset semantics must reuse the grep design,
    # not start a second ladder. The shared counter helper keeps the counting
    # semantics single-source; this pin keeps the thresholds aliased too.
    assert _GLOB_NO_HIT_HINT_AFTER == _GREP_NO_MATCH_HINT_AFTER
    assert _GLOB_NO_HIT_ESCALATE_AFTER == _GREP_NO_MATCH_ESCALATE_AFTER
    assert _GLOB_NO_FILES_SENTINEL == _BARE_EMPTY


def test_budget_note_states_the_withheld_matches_fact() -> None:
    # The nudge and the tdd-test-failure-repair skill (rule 10b, pinned in
    # test_tdd_prompt_guidance.py) must carry the same fact: a no-hit glob
    # is not proof of absence under read-denied subtrees.
    middleware = GlobGuidanceMiddleware()
    for _ in range(_GLOB_NO_HIT_HINT_AFTER):
        result = middleware.wrap_tool_call(_glob_request(), _miss())
    assert "does not prove a file is absent" in result.content
    assert "withheld, not listed" in result.content
    assert "node_modules" in result.content


# -- build-path nails --------------------------------------------------------------


def test_build_path_budget_hint_on_third_consecutive_miss(tmp_project_dir: Path) -> None:
    (tmp_project_dir / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    miss_args = {"pattern": "zzz/**/cli*", "path": "/workspace"}
    # One glob per assistant turn: within a single parallel batch the budget
    # hint lands on exactly one result but which one depends on resumption
    # order (see drive_scripted_tool_turns); a retry loop is turn-serial.
    miss_turns = [[("glob", miss_args)]] * 3

    contents = [turn[0] for turn in drive_scripted_tool_turns(tmp_project_dir, miss_turns)]

    assert all(content.startswith("No files found") for content in contents)
    assert "Glob budget" not in contents[0]
    assert "Glob budget" not in contents[1]
    assert "3 consecutive no-hit globs on /workspace" in contents[2]


def test_build_path_hit_resets_the_budget(tmp_project_dir: Path) -> None:
    (tmp_project_dir / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    miss_args = {"pattern": "zzz/**/cli*", "path": "/workspace"}
    hit_args = {"pattern": "*.txt", "path": "/workspace"}
    turns = [
        [("glob", miss_args)],
        [("glob", miss_args)],
        [("glob", hit_args)],
        [("glob", miss_args)],
    ]

    contents = [turn[0] for turn in drive_scripted_tool_turns(tmp_project_dir, turns)]

    assert "Glob budget" not in contents[0]
    assert "Glob budget" not in contents[1]
    assert "/workspace/alpha.txt" in contents[2]
    assert "Glob budget" not in contents[3]

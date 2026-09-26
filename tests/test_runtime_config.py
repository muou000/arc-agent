from __future__ import annotations

import asyncio

import pytest

from core.runtime_config import (
    resolve_runtime_config,
    runtime_config_warnings,
    validate_compile_config,
    validate_runtime_config,
)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARC_MODEL_MAX_RETRIES", "nope"),
        ("ARC_MODEL_MAX_RETRIES", "-1"),
        ("ARC_MODEL_MAX_RETRIES", "11"),
        ("ARC_MODEL_TIMEOUT", "nan"),
        ("ARC_MODEL_TIMEOUT", "inf"),
        ("ARC_PROVIDER_OUTAGE_WINDOW_SECONDS", "0"),
        ("ARC_AGENT_RECURSION_LIMIT", "19"),
        ("ARC_MAX_CONCURRENT_TASKS", "9"),
        ("ARC_WEB_PORT", "65536"),
    ],
)
def test_rejects_invalid_numeric_runtime_values(name: str, value: str) -> None:
    errors = validate_runtime_config({name: value})
    assert errors
    assert name in errors[0]
    assert value in errors[0]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARC_OPENAI_API_MODE", "responses-v2"),
        ("ARC_MODEL_STREAM_TRANSPORT", "streaming"),
        ("ARC_STRUCTURED_OUTPUT", "sometimes"),
        ("ARC_NODE_WORKTREES", "enabled-ish"),
        ("ARC_AGENT_CHECKPOINTER", "sometimes"),
        ("ARC_STAGE_PIPELINE", "disabled"),
    ],
)
def test_rejects_invalid_enum_and_boolean_runtime_values(name: str, value: str) -> None:
    errors = validate_runtime_config({name: value})
    assert errors
    assert name in errors[0]
    assert value in errors[0]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("ARC_OPENAI_API_MODE", "true", "responses"),
        ("ARC_OPENAI_API_MODE", "chat", "chat_completions"),
        ("ARC_MODEL_STREAM_TRANSPORT", "retry-only", "retry"),
        ("ARC_MODEL_STREAM_TRANSPORT", "off", "off"),
        ("ARC_STRUCTURED_OUTPUT", "force", "on"),
        ("ARC_AGENT_CHECKPOINTER", "disabled", False),
        ("ARC_STAGE_PIPELINE", "yes", True),
    ],
)
def test_preserves_documented_compatibility_values(name: str, value: str, expected: object) -> None:
    assert validate_runtime_config({name: value}) == []
    assert resolve_runtime_config({name: value})[name] == expected


def test_accepts_numeric_boundaries_and_fractional_timeouts() -> None:
    values = {
        "ARC_MODEL_TIMEOUT": "1.5",
        "ARC_MODEL_CONNECT_TIMEOUT": "1",
        "ARC_MODEL_MAX_RETRIES": "0",
        "ARC_PROVIDER_OUTAGE_THRESHOLD": "0",
        "ARC_PROVIDER_OUTAGE_WINDOW_SECONDS": "86400",
        "ARC_AGENT_RECURSION_LIMIT": "20",
        "ARC_MAX_CONCURRENT_TASKS": "8",
        "ARC_WEB_PORT": "65535",
    }
    assert validate_runtime_config(values) == []
    resolved = resolve_runtime_config(values)
    assert resolved["ARC_MODEL_TIMEOUT"] == 1.5
    assert resolved["ARC_MAX_CONCURRENT_TASKS"] == 8


def test_invalid_timeout_relationship_is_reported_with_effective_policy() -> None:
    errors = validate_runtime_config(
        {"ARC_MODEL_RETRY_DELAY": "30", "ARC_MODEL_RETRY_MAX_DELAY": "10"}
    )
    assert errors == []
    warnings = runtime_config_warnings(
        {"ARC_MODEL_RETRY_DELAY": "30", "ARC_MODEL_RETRY_MAX_DELAY": "10"}
    )
    assert len(warnings) == 1
    assert "ARC_MODEL_RETRY_DELAY" in warnings[0]
    assert "ARC_MODEL_RETRY_MAX_DELAY" in warnings[0]
    assert "effective" in warnings[0].lower()


def test_connect_timeout_cannot_exceed_request_timeout() -> None:
    errors = validate_runtime_config(
        {"ARC_MODEL_TIMEOUT": "5", "ARC_MODEL_CONNECT_TIMEOUT": "6"}
    )

    assert errors == []
    warnings = runtime_config_warnings(
        {
            "ARC_MODEL_TIMEOUT": "5",
            "ARC_MODEL_CONNECT_TIMEOUT": "6",
            "ARC_MODEL_STREAM_CHUNK_TIMEOUT": "0",
        }
    )
    assert len(warnings) == 1
    assert "ARC_MODEL_CONNECT_TIMEOUT" in warnings[0]
    assert "effective" in warnings[0].lower()


def test_unrelated_unknown_environment_variables_are_ignored() -> None:
    assert validate_runtime_config({"CUSTOM_SETTING": "invalid"}) == []


def test_resolver_rejects_invalid_values_instead_of_defaulting() -> None:
    with pytest.raises(ValueError, match="ARC_MODEL_MAX_RETRIES"):
        resolve_runtime_config({"ARC_MODEL_MAX_RETRIES": "banana"})


def test_compile_requirement_tree_rejects_invalid_config_before_runtime_write(tmp_path, monkeypatch) -> None:
    from core.workflow import ARCWorkflowManager

    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "999")

    result = asyncio.run(manager.compile_requirement_tree({"id": "ROOT"}))

    assert result["ok"] is False
    assert any("ARC_MAX_CONCURRENT_TASKS" in error for error in result["config_errors"])
    assert not (tmp_path / ".arc" / "traceability").exists()
    assert not (tmp_path / ".arc" / "processing_queue.json").exists()


def test_workflow_rejects_unknown_app_type_instead_of_defaulting_to_web(tmp_path) -> None:
    from core.workflow import ARCWorkflowManager

    with pytest.raises(ValueError, match="ARC_APP_TYPE"):
        ARCWorkflowManager(workspace_path=str(tmp_path), app_type="desktop")


def test_compile_config_includes_call_level_overrides() -> None:
    errors = validate_compile_config(app_type="desktop", web_port=65536)

    assert any("ARC_APP_TYPE" in error for error in errors)
    assert any("ARC_WEB_PORT" in error for error in errors)

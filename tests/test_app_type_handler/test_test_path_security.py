"""Generated test paths must stay inside their app-type test roots."""

from __future__ import annotations

import asyncio

from app_type_handler.android import AndroidAppType
from app_type_handler.cli import CliAppType
from app_type_handler.web import WebAppType, _build_web_group_execution, _build_web_test_execution


def _handler(handler_type):
    return handler_type("workspace", "requirements.yaml", None, lambda *_args, **_kwargs: None)


def test_web_rejects_traversal_and_shell_syntax() -> None:
    handler = _handler(WebAppType)

    assert handler.validate_test_path("e2e", "backend/test-e2e/../../outside.js")
    assert handler.validate_test_path("e2e", "backend/test-e2e/home.js & whoami &.js")
    assert handler.validate_test_path("unit", "frontend/tests/../../outside.test.ts")


def test_web_execution_builder_rejects_an_unsafe_target() -> None:
    unsafe_path = "frontend/tests/home.test.ts & whoami &.test.ts"

    try:
        _build_web_test_execution("unit", unsafe_path, "workspace")
    except ValueError as exc:
        assert "test path" in str(exc).lower()
    else:
        raise AssertionError("unsafe web test path was accepted by the execution builder")


def test_web_group_builder_rejects_an_unsafe_e2e_target() -> None:
    """E2E targets enter through the group builder; it must reject them too."""

    unsafe_path = "backend/test-e2e/home.js & whoami &.js"

    try:
        _build_web_group_execution("e2e", [unsafe_path], "workspace")
    except ValueError as exc:
        assert "test path" in str(exc).lower()
    else:
        raise AssertionError("unsafe e2e target was accepted by the group execution builder")


def test_cli_rejects_traversal() -> None:
    handler = _handler(CliAppType)

    assert handler.validate_test_path("unit", "tests/unit/../../outside_test.py")


def test_android_rejects_unsafe_test_file_before_gradle(monkeypatch) -> None:
    handler = _handler(AndroidAppType)
    called = False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        return "Exit Code: 0"

    monkeypatch.setattr("app_type_handler.android._run_android_gradle_test", fail_if_called)

    result = asyncio.run(
        handler.run_test_file(
            "unit",
            'app/src/test/java/com/example/app/unit/HomeTest.java" & whoami &.java',
        )
    )

    assert "Exit Code: 1" in result.output
    assert result.exit_code == 1
    assert called is False

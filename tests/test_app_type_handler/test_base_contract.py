from __future__ import annotations

import asyncio

from app_type_handler.base import AppTypeHandler
from app_type_handler.test_results import TestRunResult


class _MinimalHandler(AppTypeHandler):
    async def run_test_file(self, test_type: str, file_path: str) -> TestRunResult:
        return TestRunResult(exit_code=0, output="Exit Code: 0\nSTDERR:\n")

    @classmethod
    def build_stack_block(cls, *, web_port=None, android_package=None) -> str:
        del web_port, android_package
        return ""

    @classmethod
    def default_stack_summary(cls) -> str:
        return "minimal"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        del metadata_content
        return cls.default_stack_summary()


def test_default_group_runner_rejects_empty_batches() -> None:
    handler = _MinimalHandler("workspace", "requirements.yaml", None, lambda *args: None)

    result = asyncio.run(handler.run_test_group("Unit", []))

    assert result.exit_code == 1
    assert result.output.startswith("Exit Code: 1\n")
    assert "No test files were configured" in result.output


def test_default_group_runner_aggregates_per_file_results() -> None:
    """The default group runner reads per-file verdicts structurally."""

    class _MixedHandler(_MinimalHandler):
        async def run_test_file(self, test_type: str, file_path: str) -> TestRunResult:
            if "fail" in file_path:
                return TestRunResult(exit_code=1, output=f"Exit Code: 1\nFAIL {file_path}\n")
            return TestRunResult(exit_code=0, output=f"Exit Code: 0\nPASS {file_path}\n")

    handler = _MixedHandler("workspace", "requirements.yaml", None, lambda *args: None)

    result = asyncio.run(handler.run_test_group("Unit", ["tests/unit/ok_test.py", "tests/unit/fail_test.py"]))

    assert result.exit_code == 1
    assert "Exit Code: 1" in result.output
    assert "=== Test File: tests/unit/ok_test.py ===" in result.output
    assert result.failed
    assert result.passed

    passing = asyncio.run(handler.run_test_group("Unit", ["tests/unit/ok_test.py"]))
    assert passing.exit_code == 0

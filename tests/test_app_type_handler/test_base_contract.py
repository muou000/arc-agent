from __future__ import annotations

import asyncio

from app_type_handler.base import AppTypeHandler


class _MinimalHandler(AppTypeHandler):
    async def run_test_file(self, test_type: str, file_path: str) -> str:
        return "Exit Code: 0\nSTDERR:\n"

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

    assert result.startswith("Exit Code: 1\n")
    assert "No test files were configured" in result

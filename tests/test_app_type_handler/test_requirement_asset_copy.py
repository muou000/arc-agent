"""Requirement image assets must land in the served frontend before agents run.

Agents render description-referenced paths such as ``assets/logo.png`` into
components. Without a copy step they fabricate binary files with write_file
(observed: 0-byte PNGs on the 12306 benchmark), and every image-bearing
acceptance check then fails and burns TDD retries.
"""

from __future__ import annotations

import asyncio

import pytest

from app_type_handler import web as web_module
from app_type_handler.web import WebAppType


async def _noop_log(*args) -> None:
    return None


def _handler(tmp_path, requirement_path) -> WebAppType:
    handler = WebAppType.__new__(WebAppType)
    handler.workspace_path = str(tmp_path)
    handler.requirement_path = str(requirement_path)
    handler.log_cb = _noop_log
    return handler


def _make_requirements(tmp_path, with_assets: bool = True):
    requirements_dir = tmp_path / "input" / "requirements"
    requirements_dir.mkdir(parents=True)
    (requirements_dir / "requirements.yaml").write_text("id: ROOT\n", encoding="utf-8")
    if with_assets:
        assets = requirements_dir / "assets"
        assets.mkdir()
        (assets / "logo.png").write_bytes(b"\x89PNG fake")
        (assets / "banner1.jpg").write_bytes(b"\xff\xd8 fake")
    return requirements_dir / "requirements.yaml"


def test_assets_are_copied_into_public_assets(tmp_path) -> None:
    requirement_path = _make_requirements(tmp_path)
    (tmp_path / "frontend").mkdir()
    handler = _handler(tmp_path, requirement_path)

    asyncio.run(handler._copy_requirement_assets())

    copied = tmp_path / "frontend" / "public" / "assets"
    assert (copied / "logo.png").read_bytes() == b"\x89PNG fake"
    assert (copied / "banner1.jpg").read_bytes() == b"\xff\xd8 fake"


def test_existing_files_are_never_overwritten(tmp_path) -> None:
    requirement_path = _make_requirements(tmp_path)
    existing = tmp_path / "frontend" / "public" / "assets"
    existing.mkdir(parents=True)
    (existing / "logo.png").write_bytes(b"agent-owned")
    handler = _handler(tmp_path, requirement_path)

    asyncio.run(handler._copy_requirement_assets())

    assert (existing / "logo.png").read_bytes() == b"agent-owned"
    assert (existing / "banner1.jpg").read_bytes() == b"\xff\xd8 fake"


def test_missing_assets_directory_is_a_no_op(tmp_path) -> None:
    requirement_path = _make_requirements(tmp_path, with_assets=False)
    (tmp_path / "frontend").mkdir()
    handler = _handler(tmp_path, requirement_path)

    asyncio.run(handler._copy_requirement_assets())

    assert not (tmp_path / "frontend" / "public" / "assets").exists()


def test_copy_failure_is_logged_not_raised(tmp_path, monkeypatch) -> None:
    requirement_path = _make_requirements(tmp_path)
    (tmp_path / "frontend").mkdir()
    logs: list[tuple] = []

    async def log_cb(agent_name, message, status=None, node_id=None):
        logs.append((status, message))

    def broken_copy2(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(web_module.shutil, "copy2", broken_copy2)
    handler = _handler(tmp_path, requirement_path)
    handler.log_cb = log_cb

    asyncio.run(handler._copy_requirement_assets())

    assert logs and logs[0][0] == "warning"


def test_empty_requirement_path_is_a_no_op(tmp_path) -> None:
    (tmp_path / "frontend").mkdir()
    handler = _handler(tmp_path, "")

    asyncio.run(handler._copy_requirement_assets())

    assert not (tmp_path / "frontend" / "public").exists()


def test_nested_assets_are_copied_preserving_relative_paths(tmp_path) -> None:
    requirement_path = _make_requirements(tmp_path)
    nested = tmp_path / "input" / "requirements" / "assets" / "nested"
    nested.mkdir()
    (nested / "deep.png").write_bytes(b"deep")
    (tmp_path / "frontend").mkdir()
    handler = _handler(tmp_path, requirement_path)

    asyncio.run(handler._copy_requirement_assets())

    copied = tmp_path / "frontend" / "public" / "assets"
    assert (copied / "nested" / "deep.png").read_bytes() == b"deep"
    assert (copied / "logo.png").exists()

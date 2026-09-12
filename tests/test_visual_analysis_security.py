"""Visual references must stay inside the requirement asset directory."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import visual_analysis


def test_resolve_image_path_accepts_an_asset_under_requirements(tmp_path: Path) -> None:
    requirements_dir = tmp_path / "requirements"
    image = requirements_dir / "assets" / "home.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")

    resolved = visual_analysis._resolve_image_path("assets/home.png", str(tmp_path), str(requirements_dir))

    assert resolved == image.resolve()


@pytest.mark.parametrize("image_path", ["../secret.png", "../../outside.png", "C:/secret.png", "/etc/passwd"])
def test_resolve_image_path_rejects_escape_paths(tmp_path: Path, image_path: str) -> None:
    requirements_dir = tmp_path / "requirements"
    requirements_dir.mkdir()

    with pytest.raises(ValueError, match="inside the requirements directory"):
        visual_analysis._resolve_image_path(image_path, str(tmp_path), str(requirements_dir))


def test_visual_request_rejects_oversized_files(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "large.png"
    image.write_bytes(b"12345")
    monkeypatch.setattr(visual_analysis, "MAX_VISUAL_IMAGE_BYTES", 4)

    with pytest.raises(ValueError, match="too large"):
        asyncio.run(visual_analysis._request_visual_analysis(image))

"""Visual analysis caches must be scoped to the configured model endpoint."""

from __future__ import annotations

from pathlib import Path

from core import visual_analysis


def test_visual_cache_key_changes_when_model_or_endpoint_changes(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("VISUAL_MODEL", "vision-model-a")
    monkeypatch.setenv("VISUAL_BASE_URL", "https://vision-a.example/v1")
    first = visual_analysis._build_visual_cache_key(image)

    monkeypatch.setenv("VISUAL_MODEL", "vision-model-b")
    second = visual_analysis._build_visual_cache_key(image)
    monkeypatch.setenv("VISUAL_MODEL", "vision-model-a")
    monkeypatch.setenv("VISUAL_BASE_URL", "https://vision-b.example/v1")
    third = visual_analysis._build_visual_cache_key(image)

    assert second != first
    assert third != first

"""Destructive workspace cleanup must reject protected paths."""

from __future__ import annotations

from pathlib import Path

from core.path_safety import validate_clean_target


def test_clean_target_accepts_an_isolated_output_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    requirement_dir = tmp_path / "requirements"

    assert validate_clean_target(output_dir, requirement_dir, repo_root=tmp_path / "repo") is None


def test_clean_target_rejects_filesystem_root(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)

    assert "filesystem root" in (validate_clean_target(root, tmp_path / "requirements") or "")


def test_clean_target_rejects_repo_and_current_directory(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    assert "repository root" in (validate_clean_target(repo_root, tmp_path / "requirements", repo_root=repo_root) or "")
    assert "current directory" in (validate_clean_target(repo_root, tmp_path / "requirements", repo_root=tmp_path / "other") or "")


def test_clean_target_rejects_paths_overlapping_requirements(tmp_path: Path) -> None:
    requirement_dir = tmp_path / "requirements"

    assert validate_clean_target(requirement_dir, requirement_dir, repo_root=tmp_path / "repo")
    assert validate_clean_target(tmp_path, requirement_dir, repo_root=tmp_path / "repo")
    assert validate_clean_target(requirement_dir / "generated", requirement_dir, repo_root=tmp_path / "repo")

"""Project environment loading must be independent of the caller's cwd."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_arc_main_repo_root_is_the_module_directory() -> None:
    import arc_main

    assert Path(arc_main._get_repo_root()) == REPO_ROOT


def test_explicit_env_file_wins_over_the_repo_dotenv(tmp_path: Path) -> None:
    custom_env = tmp_path / "custom.env"
    custom_env.write_text("MODEL=custom-model\n", encoding="utf-8")
    child_env = os.environ.copy()
    for key in ("MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ARC_OPENAI_API_MODE"):
        child_env.pop(key, None)
    child_env["ARC_ENV_FILE"] = str(custom_env)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, arc_main; arc_main._ensure_dotenv_loaded(); print(os.environ.get('MODEL', ''))",
        ],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "custom-model"


def test_check_config_reports_the_explicit_env_file(tmp_path: Path, monkeypatch) -> None:
    custom_env = tmp_path / "custom.env"
    custom_env.write_text(
        "OPENAI_API_KEY=test-key\n"
        "OPENAI_BASE_URL=https://example.invalid/v1\n"
        "MODEL=test-model\n"
        "ARC_OPENAI_API_MODE=chat_completions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARC_ENV_FILE", str(custom_env))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("MODEL", "test-model")
    monkeypatch.setenv("ARC_OPENAI_API_MODE", "chat_completions")

    from core.config import check_config

    result = check_config()

    assert any(str(custom_env.resolve()) in item for item in result["info"])
    assert not any("No .env file found" in item for item in result["warnings"])

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


def test_missing_explicit_env_file_fails_with_a_clean_cli_error(tmp_path: Path) -> None:
    missing_env = tmp_path / "absent.env"
    child_env = os.environ.copy()
    child_env["ARC_ENV_FILE"] = str(missing_env)

    result = subprocess.run(
        [sys.executable, "arc_main.py", "doctor"],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"ARC_ENV_FILE does not exist: {missing_env}" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_compile_rejects_invalid_runtime_config_before_cleaning_output(tmp_path: Path) -> None:
    custom_env = tmp_path / "invalid.env"
    custom_env.write_text("ARC_MODEL_MAX_RETRIES=banana\n", encoding="utf-8")
    output = tmp_path / "existing-output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    child_env = os.environ.copy()
    child_env["ARC_ENV_FILE"] = str(custom_env)
    child_env.pop("ARC_MODEL_MAX_RETRIES", None)

    result = subprocess.run(
        [
            sys.executable,
            "arc_main.py",
            "compile",
            str(tmp_path / "missing-requirements.yaml"),
            "-o",
            str(output),
            "--clean",
        ],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "ARC_MODEL_MAX_RETRIES" in result.stdout
    assert "banana" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_check_config_flags_bad_model_retry_env_values(monkeypatch) -> None:
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "banana")
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "99")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "5")

    from core.config import check_config

    result = check_config()

    errors = "\n".join(result["errors"])
    assert "ARC_MODEL_TIMEOUT" in errors and "banana" in errors
    assert "ARC_MODEL_MAX_RETRIES" in errors and "99" in errors
    assert "ARC_MODEL_RETRY_DELAY" not in errors


def test_check_config_accepts_valid_model_retry_env_values(monkeypatch) -> None:
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "120")
    monkeypatch.setenv("ARC_MODEL_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "5")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "stream")

    from core.config import check_config

    result = check_config()

    assert not any("ARC_MODEL_" in item for item in result["warnings"])


def test_check_config_flags_bad_stream_transport_env_value(monkeypatch) -> None:
    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "streaming")

    from core.config import check_config

    result = check_config()

    errors = "\n".join(result["errors"])
    assert "ARC_MODEL_STREAM_TRANSPORT" in errors
    assert "streaming" in errors


def test_check_config_accepts_all_stream_transport_modes(monkeypatch) -> None:
    from core.config import check_config

    for mode in ("stream", "retry", "0", "off"):
        monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", mode)
        result = check_config()
        assert not any("ARC_MODEL_STREAM_TRANSPORT" in item for item in result["warnings"])

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

import core.logging as arc_logging


def test_configured_stdio_delivers_print_before_process_exit() -> None:
    """A pipe consumer must see ordinary output while the agent is alive."""

    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "from core.logging import configure_process_stdio; "
        "configure_process_stdio(); "
        "assert __import__('os').environ['PYTHONUNBUFFERED'] == '0'; "
        "print('ARC_LIVE_OUTPUT_1'); "
        "print('ARC_LIVE_OUTPUT_2'); "
        "import time; time.sleep(1.2)"
    )
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "0"

    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    try:
        assert process.stdout is not None
        lines = [process.stdout.readline(), process.stdout.readline()]
        elapsed = time.monotonic() - started
        assert lines == ["ARC_LIVE_OUTPUT_1\n", "ARC_LIVE_OUTPUT_2\n"]
        assert elapsed < 1.0
        assert process.poll() is None
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_stdio_configuration_failure_is_reported(monkeypatch) -> None:
    class UnsupportedStream:
        def reconfigure(self, **_kwargs) -> None:
            raise TypeError("unsupported")

        def flush(self) -> None:
            return None

    warning = io.StringIO()
    stream = UnsupportedStream()
    monkeypatch.setattr(arc_logging.sys, "stdout", stream)
    monkeypatch.setattr(arc_logging.sys, "stderr", stream)
    monkeypatch.setattr(arc_logging.sys, "__stderr__", warning)

    arc_logging.configure_process_stdio()

    diagnostic = warning.getvalue()
    assert "stdout" in diagnostic
    assert "stderr" in diagnostic


def test_stdio_wrapper_traversal_handles_falsy_wrappers_and_unexpected_errors() -> None:
    class FalsyStream:
        configured = False

        def __bool__(self) -> bool:
            return False

        def reconfigure(self, **_kwargs) -> None:
            self.configured = True

    class RaisingWrapper:
        def __init__(self, wrapped) -> None:
            self.stream = wrapped

        def reconfigure(self, **_kwargs) -> None:
            raise RuntimeError("wrapper failure")

    falsy_stream = FalsyStream()
    assert arc_logging._configure_text_stream(RaisingWrapper(falsy_stream)) is True
    assert falsy_stream.configured is True

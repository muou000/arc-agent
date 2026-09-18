from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def test_configured_stdio_delivers_print_before_process_exit() -> None:
    """A pipe consumer must see ordinary output while the agent is alive."""

    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "from core.logging import configure_process_stdio; "
        "configure_process_stdio(); "
        "print('ARC_LIVE_OUTPUT'); "
        "import time; time.sleep(1.2)"
    )
    env = os.environ.copy()
    env.pop("PYTHONUNBUFFERED", None)

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
        line = process.stdout.readline()
        elapsed = time.monotonic() - started
        assert line == "ARC_LIVE_OUTPUT\n"
        assert elapsed < 1.0
    finally:
        process.terminate()
        process.communicate(timeout=5)

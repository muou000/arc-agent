"""A/B runner for issue #214 (visual_reference dict round-trip).

The eval harness passes one command line per run:
``<requirement> -o <workspace> -t <type> --port <port> [arm argv]``.
The baseline arm carries the ``__BASELINE_214__`` marker argv; this script
strips it and dispatches the compile to the pre-#214 checkout (main @
ae0b026, includes #215 compact JSON). The candidate arm runs the fix
branch's checkout. Both checkouts share the same .env (copied from the main
workspace), so the only delta is the code.

Checkout roots come from ``ARC_EVAL_BASELINE_ROOT`` / ``ARC_EVAL_CANDIDATE_ROOT``
and default to the host paths of the recorded run below (the eval evidence in
this directory was produced with exactly these defaults on 2026-09-23); set
them to re-run the comparison from another machine or checkout layout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MAIN = Path(os.environ.get("ARC_EVAL_BASELINE_ROOT") or r"D:\code\arc-agent")
CANDIDATE = Path(os.environ.get("ARC_EVAL_CANDIDATE_ROOT") or r"D:\code\arc-visual-reference-dict-214")
MARKER = "__BASELINE_214__"


def main() -> int:
    argv = sys.argv[1:]
    checkout = MAIN if MARKER in argv else CANDIDATE
    run_argv = [item for item in argv if item != MARKER]
    command = [sys.executable, str(checkout / "arc_main.py"), "compile", *run_argv]
    print(f"[runner-214] dispatch -> {checkout} :: {' '.join(run_argv)}", flush=True)
    return subprocess.call(command, cwd=str(checkout))


if __name__ == "__main__":
    raise SystemExit(main())

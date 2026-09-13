"""Fake ARC runner for eval-harness tests.

Invoked by ``core.evals`` per the runner contract:
``python fake_eval_runner.py <requirement> -o <workspace> [extra argv]``
(no ``compile`` subcommand — custom runners receive plain run arguments).

Behavior is driven by environment variables (applied per arm by the harness):

- ``EVAL_FAKE_TOKENS``  total llm tokens across two ``llm_usage`` events (default 0)
- ``EVAL_FAKE_NODES``   comma list ``node_id:STATE`` for processing_queue.json
                        (default ``n1:PASSED,n2:PASSED``)
- ``EVAL_FAKE_EXIT``    runner exit code (default 0)
- ``EVAL_FAKE_SLEEP``   seconds to sleep before exiting (default 0)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    workspace = Path(argv[argv.index("-o") + 1])
    arc_dir = workspace / ".arc"
    arc_dir.mkdir(parents=True, exist_ok=True)

    tokens = int(os.environ.get("EVAL_FAKE_TOKENS", "0"))
    cost_per_call = (tokens / 2) * 0.000002
    usage = {
        "input": tokens // 4,
        "output": tokens // 4,
        "cache_read": 0,
        "cache_write": 0,
        "cache_write_1h": None,
        "reasoning": None,
        "total": tokens // 2,
    }
    events = [
        {"type": "runner_state", "state": "running", "timestamp": "2026-09-13 10:00:00", "message": None},
        {
            "type": "llm_usage",
            "node_id": "n1",
            "phase": "IMPLEMENT",
            "model": "fake-model",
            "api_mode": "chat_completions",
            "source": "reported",
            "usage": dict(usage),
            "cost": {"input": cost_per_call / 2, "output": cost_per_call / 2, "cache_read": 0.0, "cache_write": 0.0, "total": cost_per_call},
            "timestamp": "2026-09-13 10:00:01",
        },
        {
            "type": "llm_usage",
            "node_id": "n2",
            "phase": "IMPLEMENT",
            "model": "fake-model",
            "api_mode": "chat_completions",
            "source": "reported",
            "usage": dict(usage),
            "cost": {"input": cost_per_call / 2, "output": cost_per_call / 2, "cache_read": 0.0, "cache_write": 0.0, "total": cost_per_call},
            "timestamp": "2026-09-13 10:00:02",
        },
        {"type": "runner_state", "state": "completed", "timestamp": "2026-09-13 10:00:03", "message": None},
    ]
    (arc_dir / "runner-events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    node_states = {}
    for item in os.environ.get("EVAL_FAKE_NODES", "n1:PASSED,n2:PASSED").split(","):
        node_id, _, state = item.strip().partition(":")
        if node_id:
            node_states[node_id] = state
    (arc_dir / "processing_queue.json").write_text(
        json.dumps({"root_id": "root", "tasks": [], "node_states": node_states}),
        encoding="utf-8",
    )

    time.sleep(float(os.environ.get("EVAL_FAKE_SLEEP", "0")))
    return int(os.environ.get("EVAL_FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

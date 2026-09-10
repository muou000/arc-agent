"""Pure helpers driving ARC's post-run auto TDD re-prompt.

After the multi-node compilation pass finishes, ``core.workflow.ARCWorkflowManager``
scans ``.arc/runner-events.jsonl`` for any ``test/failed`` requirement state and
re-prompts each unique still-failing node once with a TDD-first follow-up. The
orchestration lives in the workflow; this module keeps the two decision-making
helpers pure and dependency-free (standard library only) so they can be unit
tested without importing the heavy agent/runtime stack.

The event fields consumed here (``type=requirement_state``, ``phase=test``,
``status=failed``, ``node_id``, ``message``) are written by
``arcbench_agent_runtime.events.EventClient``. Renaming any of them must be
mirrored in :func:`scan_test_failures`.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path


def scan_test_failures(runner_events_path: Path) -> list[tuple[str, str | None]]:
    """Read ``.arc/runner-events.jsonl`` and return unique ``(node_id, first_message)``
    pairs where the agent recorded a ``test/failed`` requirement state.

    Order follows first-seen in the log so retries run in the same order the agent
    emitted the failures. Empty / unparseable lines are skipped. The same node
    repeated across the run collapses to a single retry entry — the first message
    wins so the re-prompt carries the original failure detail.
    """
    if not runner_events_path.exists():
        return []
    failures: dict[str, str | None] = {}
    for line in runner_events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "requirement_state":
            continue
        if record.get("phase") != "test" or record.get("status") != "failed":
            continue
        node_id = str(record.get("node_id") or "").strip()
        if not node_id:
            continue
        failures.setdefault(node_id, record.get("message"))
    return list(failures.items())


def build_tdd_reprompt(node_id: str, message: str | None) -> str:
    """Build the TDD-first follow-up injected into a failing node's retry session.

    The text is fed to ``TestDrivenDeveloper`` as ``previous_failure_summary`` and
    deliberately spells out the red-green sequence so the agent reproduces the
    failure with a test before touching the implementation, and never weakens a
    test just to silence it. Wording matches ARC's ``tdd-test-failure-repair``
    skill: progress is reported through ``run_tests`` and the layer ladder
    ``Unit -> Integration -> E2E``, and success is declared with ``IMPLEMENTED``.
    """
    detail = message.strip() if isinstance(message, str) and message.strip() else "(no detail provided)"
    return textwrap.dedent(
        f"""
        TDD follow-up for {node_id}:
        Your previous implementation reported a test failure: {detail}

        Follow this exact TDD sequence before declaring the fix done:
        1. Add or update a failing test (or e2e scenario) that reproduces the reported failure.
        2. Run it with `run_tests` and confirm it fails for the right reason.
        3. Modify the implementation until that test passes. Do not weaken the test to make it pass.
        4. Re-run the full test surface for this requirement (Unit -> Integration -> E2E) and confirm green.
        5. Return `IMPLEMENTED` only once every scheduled layer passes. If you cannot make it pass, keep
           the failure recorded with an explicit reason instead of declaring success.

        Do not skip step 1. Do not modify tests just to silence them.
        """
    ).strip()

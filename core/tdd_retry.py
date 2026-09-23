"""Pure helpers driving ARC's post-run auto TDD re-prompt.

After the multi-node compilation pass finishes, ``core.workflow.ARCWorkflowManager``
scans ``.arc/runner-events.jsonl`` for any ``test/failed`` requirement state and
re-prompts each unique still-failing node once with a TDD-first follow-up. The
orchestration lives in the workflow; this module keeps the decision-making
helpers pure and dependency-free (standard library only) so they can be unit
tested without importing the heavy agent/runtime stack.

The event fields consumed here (``type=requirement_state``, ``phase=test``,
``status=failed``, ``node_id``, ``message``, plus the per-call
``tool_usage``/``llm_usage`` streams read by :func:`collect_attempt_facts`)
are written by ``arcbench_agent_runtime.events.EventClient``. Renaming any of
them must be mirrored in this module.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

#: Tool-name classification for the previous-attempt facts (issue #219).
#: ``MUTATING_TOOL_NAMES`` must stay in lockstep with the stage discipline's
#: write surface (``_FILE_WRITE_TOOLS`` / ``_ADDITIVE_FILE_WRITE_TOOLS`` plus
#: the delete channel) — a missed write tool would make the "zero writes"
#: fact lie to the retry. Pinned against the discipline in
#: ``tests/test_agents/test_stage_discipline.py``.
READ_ONLY_TOOL_NAMES = frozenset({"read_file", "grep", "glob", "ls"})
MUTATING_TOOL_NAMES = frozenset({"write_file", "edit_file", "append_file", "delete"})
TEST_RUN_TOOL_NAMES = frozenset({"run_tests"})

#: ``tool_usage``/``llm_usage`` events carry the phase of the session that
#: made the call; the implement phase's sessions report this value.
IMPLEMENT_PHASE = "IMPLEMENT"


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


def collect_attempt_facts(
    runner_events_path: Path,
    node_id: str,
    *,
    start_line: int = 0,
) -> dict[str, int]:
    """Aggregate the previous implementation attempt's objective counters.

    Reads the raw per-call event streams (``tool_usage`` / ``llm_usage``) the
    runtime middleware appends as calls happen, so the numbers survive even a
    ``GraphRecursionError`` crash that skips the ``tdd_handoff`` session write.
    Only the node's IMPLEMENT-phase calls count — DESIGN/TESTGEN work belongs
    to a different attempt.

    ``start_line`` skips the first N lines of the JSONL (the cursor recorded
    when the previous auto retry was queued) so a repeated auto retry measures
    only the latest attempt instead of double-counting earlier ones. A cursor
    beyond the current file length (recreated events file) is ignored; a
    cursor exactly at EOF scans nothing and returns zero counters. The
    returned ``end_line`` is the file's line count at scan time — the caller
    stores it as the next attempt's cursor.
    """
    facts: dict[str, int] = {
        "model_calls": 0,
        "tool_calls": 0,
        "read_only_calls": 0,
        "run_tests_calls": 0,
        "successful_writes": 0,
        "end_line": 0,
    }
    if not runner_events_path.exists():
        return facts
    lines = runner_events_path.read_text(encoding="utf-8").splitlines()
    facts["end_line"] = len(lines)
    if start_line < 0 or start_line > len(lines):
        start_line = 0
    target = node_id.strip()
    for line in lines[start_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or str(record.get("node_id") or "").strip() != target:
            continue
        record_type = record.get("type")
        if record_type == "llm_usage" and record.get("phase") == IMPLEMENT_PHASE:
            facts["model_calls"] += 1
        elif record_type == "tool_usage" and record.get("phase") == IMPLEMENT_PHASE:
            facts["tool_calls"] += 1
            tool = str(record.get("tool") or "").strip()
            if tool in READ_ONLY_TOOL_NAMES:
                facts["read_only_calls"] += 1
            elif tool in TEST_RUN_TOOL_NAMES:
                facts["run_tests_calls"] += 1
            elif tool in MUTATING_TOOL_NAMES and str(record.get("status") or "").strip() == "ok":
                facts["successful_writes"] += 1
    return facts


def build_tdd_reprompt(
    node_id: str,
    message: str | None,
    *,
    handoff: dict | None = None,
    attempt_facts: dict | None = None,
) -> str:
    """Build the TDD-first follow-up injected into a failing node's retry session.

    The text is fed to ``TestDrivenDeveloper`` as ``previous_failure_summary`` and
    deliberately spells out the red-green sequence so the agent reproduces the
    failure with a test before touching the implementation, and never weakens a
    test just to silence it. Wording matches ARC's ``tdd-test-failure-repair``
    skill: progress is reported through ``run_tests`` and the layer ladder
    ``Unit -> Integration -> E2E``, and success is declared with ``IMPLEMENTED``.

    ``handoff`` is the node session's ``tdd_handoff`` from the failed round.
    Its fingerprint history and layer usage are appended as "already tried"
    evidence: a retried session starts with fresh budgets, and without that
    evidence it re-derives - or repeats - hypotheses the previous round
    already burned its budget on.

    ``attempt_facts`` is the previous attempt's objective record from
    :func:`collect_attempt_facts`. It tells the retry what the previous round
    actually spent (model calls, read-only calls, run_tests calls) and whether
    it produced any change at all — because the retry resumes the same
    checkpointer thread and inherits the previous conversation's bulk, which
    can otherwise pass for progress (issue #219: a 300-step read-only storm
    replayed itself for another full budget).
    """
    detail = message.strip() if isinstance(message, str) and message.strip() else "(no detail provided)"
    evidence_lines = "\n".join(
        part
        for part in (_attempt_facts_lines(attempt_facts), _handoff_evidence_lines(handoff))
        if part
    )
    evidence_block = ("\n" + evidence_lines + "\n") if evidence_lines else ""
    return textwrap.dedent(
        f"""
        TDD follow-up for {node_id}:
        Your previous implementation reported a test failure: {detail}
        {evidence_block}
        Follow this exact TDD sequence before declaring the fix done:
        1. Add or update a failing test (or e2e scenario) that reproduces the reported failure.
        2. Run it with `run_tests` and confirm it fails for the right reason.
        3. Modify the implementation until that test passes. Do not weaken the test to make it pass.
        4. Re-run the full test surface for this requirement (Unit -> Integration -> E2E) and confirm green.
        5. Return `IMPLEMENTED` only once every scheduled layer passes. If you cannot make it pass, keep
           the failure recorded with an explicit reason instead of declaring success.

        Do not skip step 1. Do not modify tests just to silence them. Do not repeat an approach
        already listed in the previous-round evidence above.
        """
    ).strip()


def _attempt_facts_lines(facts: dict | None) -> str:
    """Render the previous attempt's objective counters, if there is any signal.

    All-zero facts mean the node never reached IMPLEMENT — an empty record
    would only be noise, so the block is omitted entirely.
    """

    if not isinstance(facts, dict):
        return ""

    def count(key: str) -> int:
        try:
            return int(facts.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    model_calls = count("model_calls")
    tool_calls = count("tool_calls")
    if model_calls <= 0 and tool_calls <= 0:
        return ""

    def calls(value: int, noun: str) -> str:
        return f"{value} {noun}{'s' if value != 1 else ''}"

    run_tests_calls = count("run_tests_calls")
    lines = [
        (
            "- Previous attempt record (objective, from the run log): "
            f"{calls(model_calls, 'model call')}, "
            f"{calls(tool_calls, 'tool call')} "
            f"({count('read_only_calls')} read-only, "
            f"{calls(run_tests_calls, 'run_tests call')}, "
            f"{calls(count('successful_writes'), 'successful file write')})."
        )
    ]
    if count("successful_writes") == 0:
        lines.append(
            "- The previous attempt made NO successful file edits: nothing is fixed yet, and repeating "
            "its read-only exploration only burns this round's fresh budget. Change the approach."
        )
    return "\n".join(lines)


def _handoff_evidence_lines(handoff: dict | None) -> str:
    """Render the previous round's fingerprint/budget evidence, if any."""

    if not isinstance(handoff, dict):
        return ""
    blocks: list[str] = []
    usage = handoff.get("layer_usage")
    if isinstance(usage, dict) and usage:
        spent = ", ".join(
            f"{layer}: {count}/10 run_tests calls" for layer, count in sorted(usage.items())
        )
        blocks.append(f"- Previous round budgets spent ({spent}). Budgets are fresh this round.")
    fingerprints = handoff.get("fingerprint_history")
    if isinstance(fingerprints, dict) and fingerprints:
        rows = []
        for layer, entries in sorted(fingerprints.items()):
            if isinstance(entries, list) and entries:
                repeated = len(entries) > 1 and len(set(entries)) == 1
                marker = " (unchanged across runs - the hypothesis was wrong, do not retry it)" if repeated else ""
                rows.append(f"  - {layer}: {entries[-1]}{marker}")
        if rows:
            blocks.append(
                "- Failure fingerprints from the previous round (already tried):\n"
                + "\n".join(rows)
            )
    return "\n".join(blocks)

"""Think-prefix recovery for fallback-payload manifest parsing.

Regression coverage for the 2026-09-22 arc-output-serial-1 run
(easy-ticketbooking, main@044c7f8): the green-baseline repair turn skipped
the structured-output channel and answered with a reasoning block
(``&&!...**`` prefix carrying an ``import { describe, it, expect }`` snippet)
followed by a complete, valid manifest JSON with no code fences.
``_json_candidates``' brace-span heuristic took the FIRST ``{`` (inside the
think block, a code snippet brace) to the LAST ``}`` (manifest tail), which
never parses; the whole-text candidate starts with the think prefix and also
fails. Every candidate failed, the fallback payload kept only
``summary``/``_raw_final_message``, ``normalize_test_manifest_payload``
returned ``[]``, and DESIGN was failed for "removing every owned coverage
witness" even though the repair manifest was complete and correct.
"""

from __future__ import annotations

from agents.results import normalize_test_manifest_payload
from agents.runtime.runners import extract_payload, parse_json_payload

# Shape of the arc-output-serial-1 REQ-2 repair answer: a reasoning prefix
# whose code snippet carries the document's first `{`, then the manifest with
# no fences anywhere.
THINK_WITH_BRACE_PREFIX = (
    "&&! I need to rework the green baseline tests. The LoginPage test passed "
    "against the skeleton, so I'll rewrite it to drive the real contract.\n"
    "The test file imports its harness like `import { describe, it, expect } "
    "from 'vitest'` and targets the login form's owned outcome.\n"
    "Let me plan the replacement assertions before writing the manifest.\n"
    "&&!\n"
    "**Green baseline rework**\n\n"
)

THINK_NO_BRACE_PREFIX = (
    "&&! The login page test passed the skeleton baseline because it only "
    "asserted placeholder render behavior. I will rewrite it to assert the "
    "owned login outcome.\n&&!\n"
)

MANIFEST = (
    "{\n"
    '  "summary": "green-baseline rework: rewrite LoginPage spec to owned contract",\n'
    '  "tests": [\n'
    "    {\n"
    '      "test_id": "REQ-2-UNIT-LoginService-01",\n'
    '      "file_path": "frontend/tests/unit/loginService.test.ts",\n'
    '      "type": "unit",\n'
    '      "coverage_scope": "owned"\n'
    "    },\n"
    "    {\n"
    '      "test_id": "REQ-2-E2E-Login-01",\n'
    '      "file_path": "frontend/tests/e2e/login.spec.ts",\n'
    '      "type": "e2e",\n'
    '      "coverage_scope": "owned"\n'
    "    }\n"
    "  ]\n"
    "}"
)

EXPECTED_TEST_IDS = ["REQ-2-UNIT-LoginService-01", "REQ-2-E2E-Login-01"]


def _final_result(text: str) -> dict[str, object]:
    """Build a minimal agent result dict shaped like ``session.invoke`` output."""

    return {"messages": [{"role": "assistant", "content": text}]}


def test_parse_json_payload_recovers_manifest_with_brace_in_think_prefix() -> None:
    # The bug: first `{` sits inside the think block's code snippet, so the
    # brace-span candidate spans think-prefix-to-manifest-tail and never
    # parses. The manifest itself is complete and valid.
    parsed = parse_json_payload(THINK_WITH_BRACE_PREFIX + MANIFEST)
    assert parsed is not None
    assert [item["test_id"] for item in parsed["tests"]] == EXPECTED_TEST_IDS


def test_parse_json_payload_recovers_manifest_with_plain_think_prefix() -> None:
    # No `{` in the think block: the brace-span heuristic today already takes
    # the manifest's first `{` to its last `}`... but the span crosses the
    # `**` markdown line and prose between prefix and manifest only when the
    # prefix has no brace — nail the current-good shape so a future change to
    # candidate ordering cannot regress it.
    parsed = parse_json_payload(THINK_NO_BRACE_PREFIX + MANIFEST)
    assert parsed is not None
    assert [item["test_id"] for item in parsed["tests"]] == EXPECTED_TEST_IDS


def test_extract_payload_recovers_manifest_from_fallback_path() -> None:
    # End-to-end over the fallback path: no structured_response key, final
    # message carries the think prefix + manifest.
    payload = extract_payload(_final_result(THINK_WITH_BRACE_PREFIX + MANIFEST))
    assert [item["test_id"] for item in payload["tests"]] == EXPECTED_TEST_IDS
    assert "summary" in payload


def test_normalize_manifest_payload_recovers_from_raw_final_message() -> None:
    # The last-resort consumer-side fallback: even when the parser could not
    # restore a dict (e.g. an unknown prefix shape), the manifest JSON still
    # sits verbatim in the final-message text the fallback payload preserves.
    payload = {
        "summary": THINK_WITH_BRACE_PREFIX + MANIFEST,
        "_raw_final_message": '{"content": "irrelevant debug dump"}',
    }
    tests = normalize_test_manifest_payload(payload)
    assert [item["test_id"] for item in tests] == EXPECTED_TEST_IDS


def test_normalize_manifest_payload_recovers_from_raw_dump_when_summary_is_empty() -> None:
    # The gate-and-source alignment rule from the interface fallback: an
    # adapter that only populates _raw_final_message still recovers. The dump
    # is JSON-encoded, so the embedded think-prefixed manifest is escaped.
    import json as _json

    payload = {
        "summary": "",
        "_raw_final_message": _json.dumps({"content": THINK_WITH_BRACE_PREFIX + MANIFEST}),
    }
    tests = normalize_test_manifest_payload(payload)
    assert [item["test_id"] for item in tests] == EXPECTED_TEST_IDS


def test_normalize_manifest_payload_ignores_non_manifest_salvage_objects() -> None:
    # Salvage may recover prose-side objects (the code snippet object from the
    # think block); only objects that look like test rows may survive.
    payload = {
        "summary": 'muse: {"describe": "it"} then {"tests": [{"test_id": "T-1", '
        '"file_path": "a.test.ts", "type": "unit"}]}',
    }
    tests = normalize_test_manifest_payload(payload)
    assert [item["test_id"] for item in tests] == ["T-1"]


def test_parse_json_payload_still_fails_on_think_prefix_with_truncated_manifest() -> None:
    # Whole-document repair must not fabricate a payload from a cut-off
    # manifest; the salvage fallbacks stay silent too — the scanner only
    # keeps objects that still parse, and an unclosed manifest emits nothing.
    truncated = THINK_WITH_BRACE_PREFIX + '{"summary": "s", "tests": [{"test_id": "T"'
    assert parse_json_payload(truncated) is None


def test_extract_payload_still_prefers_structured_response() -> None:
    # The structured-output channel stays authoritative: a think-prefixed
    # final message must never override a real structured response.
    result = {
        "structured_response": {"tests": [{"test_id": "T-STRUCT", "file_path": "b.test.ts", "type": "unit"}]},
        "messages": [{"role": "assistant", "content": THINK_WITH_BRACE_PREFIX + MANIFEST}],
    }
    payload = extract_payload(result)
    assert [item["test_id"] for item in payload["tests"]] == ["T-STRUCT"]

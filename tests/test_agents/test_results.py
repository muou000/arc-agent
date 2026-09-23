"""Unit tests for the test-manifest payload helpers in ``agents/results``.

The green-baseline repair loop must tell "the model answered but no manifest
could be parsed out of the payload" apart from "the model explicitly declared
an empty manifest": the former is a retryable defect, the latter a decision
(issue #172).
"""

from agents.results import normalize_test_manifest_payload, payload_declares_test_manifest


def test_payload_declares_test_manifest_for_declared_structures() -> None:
    # Explicit empty manifest: declared.
    assert payload_declares_test_manifest({"tests": []}) is True
    # Normal list: declared.
    assert payload_declares_test_manifest({"tests": [{"test_id": "T1", "type": "Unit"}]}) is True
    # The items fallback key: declared.
    assert payload_declares_test_manifest({"items": []}) is True
    # A bare test item without any manifest key: declared (single-item unwrap).
    assert payload_declares_test_manifest({"test_id": "T1", "file_path": "a.js"}) is True


def test_payload_declares_test_manifest_rejects_unparseable_payloads() -> None:
    # The runner's prose fallback shape carries no manifest structure.
    assert (
        payload_declares_test_manifest({"summary": "I deleted the tests.", "_raw_final_message": "..."})
        is False
    )
    assert payload_declares_test_manifest({}) is False
    # Declared but not a list: nothing parseable was answered.
    assert payload_declares_test_manifest({"tests": "I deleted the tests."}) is False
    assert payload_declares_test_manifest({"tests": None}) is False
    # A declared list whose items all fail to parse as test entries is
    # parse damage, not a decision to return zero tests (#172 boundary).
    assert payload_declares_test_manifest({"tests": ["junk"]}) is False
    assert payload_declares_test_manifest({"tests": [{"foo": 1}]}) is False


def test_normalize_and_declare_agree_on_the_boundary() -> None:
    """Declared payloads never depend on the undeclared-payload collapse of
    ``normalize_test_manifest_payload``; undeclared ones normalize to ``[]``
    exactly as before (the main `run` path keeps that semantics)."""

    declared_empty = {"tests": []}
    undeclared = {"summary": "gibberish", "_raw_final_message": "..."}
    malformed = {"tests": "nope"}
    junk_items = {"tests": ["junk", {"foo": 1}]}

    for payload in (declared_empty, undeclared, malformed, junk_items):
        assert normalize_test_manifest_payload(payload) == []
    assert payload_declares_test_manifest(declared_empty) is True
    assert payload_declares_test_manifest(undeclared) is False
    assert payload_declares_test_manifest(malformed) is False
    assert payload_declares_test_manifest(junk_items) is False

from __future__ import annotations

from agents.tools.test_manifest import CANONICAL_TEST_TYPES as MANIFEST_TEST_TYPES
from agents.tools.test_manifest import canonical_test_type as manifest_test_type
from core.phases import TDD_BATCH_ORDER, canonical_test_type as phase_test_type
from core.test_types import CANONICAL_TEST_TYPES, canonical_test_type


def test_test_layer_vocabulary_has_one_owner() -> None:
    assert TDD_BATCH_ORDER is CANONICAL_TEST_TYPES
    assert MANIFEST_TEST_TYPES is CANONICAL_TEST_TYPES
    assert phase_test_type is canonical_test_type
    for value, expected in (("unit", "Unit"), (" INTEGRATION ", "Integration"), ("E2E", "E2E"), ("other", None)):
        assert canonical_test_type(value) == expected
        assert manifest_test_type(value) == expected
        assert phase_test_type(value) == expected

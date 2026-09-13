"""Tests for ``agents.model.costing``: benchmark catalog pricing (CNY / 1M tokens).

The catalog is a closed set: names match case-insensitively and exactly, and
models without an entry are unpriced (``compute_model_cost`` returns ``None``).
"""

from __future__ import annotations

import pytest

from agents.model.costing import ModelCost, compute_model_cost, resolve_model_cost


class TestResolveModelCost:
    def test_exact_catalog_match(self) -> None:
        assert resolve_model_cost("minimax-m3") == ModelCost(input=2.1, output=8.4, cache_read=0.42)

    def test_minimax_m3_casing_variants_are_equivalent(self) -> None:
        # The platform sends "MiniMax-M3"; the catalog lists "minimax-m3".
        assert resolve_model_cost("MiniMax-M3") == resolve_model_cost("minimax-m3")

    def test_case_and_whitespace_are_normalized(self) -> None:
        assert resolve_model_cost("  GLM-5.3 ") == resolve_model_cost("glm-5.3")

    def test_models_outside_the_catalog_are_unpriced(self) -> None:
        assert resolve_model_cost("gpt-4o") is None
        assert resolve_model_cost("totally-unknown-model") is None
        assert resolve_model_cost("") is None
        # No fuzzy matching: a near-miss name must not inherit catalog pricing.
        assert resolve_model_cost("minimax-m30") is None
        assert resolve_model_cost("kimi-k3-preview") is None


class TestCatalogRates:
    """The benchmark model catalog (CNY / 1M tokens, 2026-09)."""

    def test_all_catalog_entries(self) -> None:
        expected = {
            "deepseek-v4-flash": (3.0, 9.0, 0.1),
            "deepseek-v4-pro": (9.0, 27.0, 0.3),
            "glm-5.2": (8.0, 28.0, 2.0),
            "glm-5.3": (8.0, 28.0, 2.0),
            "kimi-k3": (20.0, 100.0, 2.0),
            "minimax-m3": (2.1, 8.4, 0.42),
            "qwen3.6-flash": (1.2, 7.2, 1.2),
            "qwen3.6-plus": (2.0, 12.0, 2.0),
            "qwen3.7-max": (12.0, 36.0, 2.4),
            "qwen3.7-plus": (2.0, 8.0, 0.4),
            "qwen3.8-max": (12.0, 36.0, 1.5),
        }
        assert set(resolve_model_cost.__globals__["_BUILTIN_MODEL_COSTS"]) == set(expected)
        for name, (input_rate, output_rate, cache_read) in expected.items():
            resolved = resolve_model_cost(name)
            assert resolved == ModelCost(input=input_rate, output=output_rate, cache_read=cache_read), name

    def test_minimax_m3_cost_math(self) -> None:
        cost = compute_model_cost(
            "MiniMax-M3",
            {"input": 2_100_000, "output": 1_000_000, "cache_read": 420_000},
        )
        assert cost == {
            "input": pytest.approx(2.1 * 2.1),
            "output": pytest.approx(8.4),
            "cache_read": pytest.approx(0.42 * 0.42),
            "cache_write": 0.0,
            "total": pytest.approx(2.1 * 2.1 + 8.4 + 0.42 * 0.42),
        }


class TestComputeModelCost:
    def test_basic_cost_semantics(self) -> None:
        cost = compute_model_cost(
            "kimi-k3",
            {"input": 1_000_000, "output": 500_000, "cache_read": 250_000, "cache_write": 0},
        )
        assert cost is not None
        assert cost["input"] == pytest.approx(20.0)
        assert cost["output"] == pytest.approx(50.0)
        assert cost["cache_read"] == pytest.approx(0.5)
        assert cost["cache_write"] == 0.0
        assert cost["total"] == pytest.approx(cost["input"] + cost["output"] + cost["cache_read"])

    def test_unpriced_model_returns_none(self) -> None:
        assert compute_model_cost("gpt-4o", {"input": 10, "output": 10}) is None

    def test_cache_writes_bill_at_input_rate(self) -> None:
        # The catalog lists no cache-write price; cache writes are input tokens.
        cost = compute_model_cost("minimax-m3", {"input": 0, "output": 0, "cache_write": 100_000})
        assert cost is not None
        assert cost["cache_write"] == pytest.approx(2.1 * 0.1)
        assert cost["total"] == pytest.approx(cost["cache_write"])

    def test_negative_and_missing_usage_values_count_as_zero(self) -> None:
        cost = compute_model_cost("glm-5.3", {"input": -5, "output": None, "cache_read": "oops"})
        assert cost == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "total": 0.0}

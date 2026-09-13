"""Model pricing table and cost computation for recorded LLM usage.

Rates are CNY per million tokens and mirror the ARC-Bench reference (pi)
``ModelCost`` semantics for flat-rate entries:

- cached input tokens bill at ``cache_read``;
- the catalog lists no separate cache-write price, so cache writes bill at the
  input rate.

The table mirrors the benchmark model catalog (DeepSeek / Z.AI / Moonshot /
MiniMax / Qwen, as of 2026-09) and is a closed set: names match
case-insensitively and exactly (``MiniMax-M3`` == ``minimax-m3``), and models
without an entry report no cost — :func:`compute_model_cost` returns ``None``
so callers can surface unpriced calls instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCost:
    """CNY per million tokens for one catalog model."""

    input: float
    output: float
    cache_read: float = 0.0


# Benchmark model catalog, CNY per 1M tokens as of 2026-09; keys match the
# model name sent to the API (resolved case-insensitively). The catalog lists
# no cache-write prices, so cache writes bill at the input rate.
#
# Notes on individual entries:
# - qwen3.6-* list no cache-hit price; their cache_read rate equals the input
#   rate on purpose ("no cache discount"), not a missing price — do not
#   "fix" it to 0 or infer a discount for them.
# - All other entries carry the catalog's explicit cache-read discount.
_BUILTIN_MODEL_COSTS: dict[str, ModelCost] = {
    "deepseek-v4-flash": ModelCost(input=3.0, output=9.0, cache_read=0.1),
    "deepseek-v4-pro": ModelCost(input=9.0, output=27.0, cache_read=0.3),
    "glm-5.2": ModelCost(input=8.0, output=28.0, cache_read=2.0),
    "glm-5.3": ModelCost(input=8.0, output=28.0, cache_read=2.0),
    "kimi-k3": ModelCost(input=20.0, output=100.0, cache_read=2.0),
    "minimax-m3": ModelCost(input=2.1, output=8.4, cache_read=0.42),
    "qwen3.6-flash": ModelCost(input=1.2, output=7.2, cache_read=1.2),
    "qwen3.6-plus": ModelCost(input=2.0, output=12.0, cache_read=2.0),
    "qwen3.7-max": ModelCost(input=12.0, output=36.0, cache_read=2.4),
    "qwen3.7-plus": ModelCost(input=2.0, output=8.0, cache_read=0.4),
    "qwen3.8-max": ModelCost(input=12.0, output=36.0, cache_read=1.5),
}


def compute_model_cost(model: str, usage: Mapping[str, object]) -> dict[str, float] | None:
    """Cost breakdown (CNY) for one call, or ``None`` when the model is unpriced.

    ``usage`` uses the canonical token semantics of
    :mod:`agents.model.usage_capture` (``input`` excludes cache tokens).
    """
    rates = resolve_model_cost(model)
    if rates is None:
        return None

    input_tokens = _usage_int(usage, "input")
    output_tokens = _usage_int(usage, "output")
    cache_read = _usage_int(usage, "cache_read")
    cache_write = _usage_int(usage, "cache_write")

    cost_input = rates.input / 1_000_000 * input_tokens
    cost_output = rates.output / 1_000_000 * output_tokens
    cost_cache_read = rates.cache_read / 1_000_000 * cache_read
    cost_cache_write = rates.input / 1_000_000 * cache_write
    return {
        "input": cost_input,
        "output": cost_output,
        "cache_read": cost_cache_read,
        "cache_write": cost_cache_write,
        "total": cost_input + cost_output + cost_cache_read + cost_cache_write,
    }


def resolve_model_cost(model: str) -> ModelCost | None:
    """Resolve pricing for ``model`` from the catalog, or ``None`` if absent."""

    return _BUILTIN_MODEL_COSTS.get(str(model or "").strip().lower())


def _usage_int(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key)
    if value is None:
        return 0
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0

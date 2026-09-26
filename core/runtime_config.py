"""Central validation and normalization for ARC runtime configuration.

The runtime reads configuration from several modules.  This module is the
startup contract for those values: parsers may keep their historical fallback
for direct library callers, but compilation validates the same environment
before it can mutate a workspace or call a provider.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
BOOL_VALUES = TRUE_VALUES | FALSE_VALUES | {"disabled"}

OPENAI_API_MODE_ALIASES = {
    "responses": "responses",
    "response": "responses",
    "responses_api": "responses",
    "1": "responses",
    "true": "responses",
    "yes": "responses",
    "on": "responses",
    "chat_completions": "chat_completions",
    "chat_completion": "chat_completions",
    "chat/completions": "chat_completions",
    "chat": "chat_completions",
    "0": "chat_completions",
    "false": "chat_completions",
    "no": "chat_completions",
    "off": "chat_completions",
}

STREAM_TRANSPORT_ALIASES = {
    "stream": "stream",
    "retry": "retry",
    "retry-only": "retry",
    "on-failure": "retry",
    "on_failure": "retry",
    "off": "off",
    "0": "off",
    "false": "off",
    "no": "off",
}

STRUCTURED_OUTPUT_ALIASES = {
    "auto": "auto",
    "on": "on",
    "force": "on",
    "1": "on",
    "true": "on",
    "yes": "on",
    "off": "off",
    "0": "off",
    "false": "off",
    "no": "off",
}

_NUMERIC_SPECS: dict[str, tuple[str, float, float, float | int]] = {
    "ARC_WEB_PORT": ("int", 1, 65535, 3301),
    "ARC_MODEL_TIMEOUT": ("float", 1, 3600, 600.0),
    "ARC_MODEL_CONNECT_TIMEOUT": ("float", 1, 600, 15.0),
    "ARC_MODEL_MAX_RETRIES": ("int", 0, 10, 3),
    "ARC_MODEL_RETRY_DELAY": ("float", 0, 3600, 5.0),
    "ARC_MODEL_RETRY_MAX_DELAY": ("float", 0, 3600, 60.0),
    "ARC_MODEL_MAX_CONSECUTIVE_FAILURES": ("int", 0, 100, 5),
    "ARC_MODEL_STREAM_CHUNK_TIMEOUT": ("float", 0, 3600, 90.0),
    "ARC_PROVIDER_OUTAGE_THRESHOLD": ("int", 0, 100, 1),
    "ARC_PROVIDER_OUTAGE_WINDOW_SECONDS": ("int", 1, 86400, 300),
    "ARC_MAX_CONCURRENT_TASKS": ("int", 1, 8, 3),
    "ARC_AFFINITY_DEPTH": ("int", 1, float("inf"), 1),
    "ARC_VISUAL_PRECOMPUTE_CONCURRENCY": ("int", 1, 8, 4),
    "ARC_VISUAL_ANALYSIS_CONCURRENCY": ("int", 1, 8, 4),
    "ARC_AGENT_RECURSION_LIMIT": ("int", 20, float("inf"), 300),
}

_BOOL_DEFAULTS: dict[str, bool] = {
    "ARC_DEBUG": False,
    "ARC_SKIP_BROWSER_INSTALL": False,
    "ARC_MODEL_STREAM_USAGE": True,
    "ARC_NODE_WORKTREES": False,
    "ARC_DESIGN_GATE_PIPELINE": False,
    "ARC_STAGE_PIPELINE": True,
    "ARC_DESIGN_BOOT_SMOKE": True,
    "ARC_MERGE_ARBITRATION": False,
    "ARC_REBASE_ON_MERGE": False,
    "ARC_VISUAL_PRECOMPUTE": True,
    "ARC_AUTO_TDD_RETRY": True,
    "ARC_TDD_RETRY_FRESH_THREAD": False,
    "ARC_AGENT_CHECKPOINTER": True,
    "ARC_AGENT_FORCE_RESPONSES_STREAM": False,
    "ARC_DEBUG_AGENT_TRACE": False,
}

_ENUM_ALIASES: dict[str, Mapping[str, Any]] = {
    "ARC_APP_TYPE": {"web": "web", "android": "android", "cli": "cli"},
    "ARC_OPENAI_API_MODE": OPENAI_API_MODE_ALIASES,
    "ARC_MODEL_STREAM_TRANSPORT": STREAM_TRANSPORT_ALIASES,
    "ARC_STRUCTURED_OUTPUT": STRUCTURED_OUTPUT_ALIASES,
    "ARC_AGENT_STREAM_VERSION": {"v1": "v1", "v2": "v2"},
    "ARC_LOG_COLOR": {**{value: True for value in TRUE_VALUES}, **{value: False for value in FALSE_VALUES}, "auto": "auto"},
    "ARC_OPENAI_SSE_TEXT_COMPAT": {**{value: True for value in TRUE_VALUES}, **{value: False for value in FALSE_VALUES}, "auto": "auto"},
}

RUNTIME_CONFIG_ENV_VARS = tuple(
    sorted(set(_NUMERIC_SPECS) | set(_BOOL_DEFAULTS) | set(_ENUM_ALIASES))
)


def validate_compile_config(*, app_type: Any = None, web_port: Any = None) -> list[str]:
    """Validate process configuration plus compile-call overrides."""

    errors = validate_runtime_config()
    if web_port is not None:
        errors.extend(validate_runtime_config({"ARC_WEB_PORT": str(web_port)}))
    if app_type is not None:
        errors.extend(validate_runtime_config({"ARC_APP_TYPE": app_type}))
    return errors


def _raw_values(values: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return os.environ if values is None else values


def _text(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _parse_number(name: str, raw: Any, kind: str) -> float | int:
    text = str(raw).strip()
    if kind == "int":
        if not text or any(character in text for character in ".eE"):
            raise ValueError
        return int(text)
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _format_range(low: float, high: float) -> str:
    if math.isinf(high):
        return f">= {low:g}"
    if low == high:
        return f"{low:g}"
    return f"{low:g}-{high:g}"


def validate_runtime_config(values: Mapping[str, Any] | None = None) -> list[str]:
    """Return startup errors for known runtime configuration values.

    Empty values are treated as unset so defaults remain compatible. Unknown
    variables are intentionally ignored because ARC passes through unrelated
    provider and application settings.
    """

    source = _raw_values(values)
    errors: list[str] = []
    parsed: dict[str, float | int] = {}

    for name, (kind, low, high, _default) in _NUMERIC_SPECS.items():
        raw = source.get(name, "")
        if str(raw).strip() == "":
            continue
        try:
            value = _parse_number(name, raw, kind)
        except (TypeError, ValueError):
            errors.append(f"{name}={raw!r} is invalid; expected {_format_range(low, high)}.")
            continue
        if value < low or value > high:
            errors.append(
                f"{name}={raw!r} is outside the allowed range {_format_range(low, high)}."
            )
            continue
        parsed[name] = value

    for name, aliases in _ENUM_ALIASES.items():
        raw = source.get(name, "")
        text = _text(raw)
        if text and text not in aliases:
            choices = ", ".join(sorted(aliases))
            errors.append(f"{name}={raw!r} is invalid; expected one of: {choices}.")

    for name, _default in _BOOL_DEFAULTS.items():
        raw = source.get(name, "")
        text = _text(raw)
        allowed_values = BOOL_VALUES if name == "ARC_AGENT_CHECKPOINTER" else (TRUE_VALUES | FALSE_VALUES)
        if text and text not in allowed_values:
            expected = (
                "0, 1, false, true, no, yes, off, on, disabled"
                if name == "ARC_AGENT_CHECKPOINTER"
                else "0, 1, false, true, no, yes, off, on"
            )
            errors.append(
                f"{name}={raw!r} is invalid; expected one of: {expected}."
            )

    return errors


def runtime_config_warnings(values: Mapping[str, Any] | None = None) -> list[str]:
    """Describe compatibility clamps and their effective runtime values."""

    source = _raw_values(values)
    errors = validate_runtime_config(source)
    if errors:
        return []
    config = resolve_runtime_config(source)
    warnings: list[str] = []
    request_timeout = config["ARC_MODEL_TIMEOUT"]

    connect_timeout = config["ARC_MODEL_CONNECT_TIMEOUT"]
    if connect_timeout > request_timeout:
        warnings.append(
            f"ARC_MODEL_CONNECT_TIMEOUT={source.get('ARC_MODEL_CONNECT_TIMEOUT')!r}; "
            f"effective connect timeout is {request_timeout:g}s (bounded by ARC_MODEL_TIMEOUT)."
        )
    chunk_timeout = config["ARC_MODEL_STREAM_CHUNK_TIMEOUT"]
    if chunk_timeout > request_timeout:
        raw_chunk_timeout = source.get("ARC_MODEL_STREAM_CHUNK_TIMEOUT", "")
        displayed_chunk_timeout = raw_chunk_timeout if str(raw_chunk_timeout).strip() else "<default>"
        warnings.append(
            f"ARC_MODEL_STREAM_CHUNK_TIMEOUT={displayed_chunk_timeout!r}; "
            f"effective chunk timeout is {request_timeout:g}s (bounded by ARC_MODEL_TIMEOUT)."
        )

    retry_delay = config["ARC_MODEL_RETRY_DELAY"]
    max_delay = config["ARC_MODEL_RETRY_MAX_DELAY"]
    if retry_delay > max_delay:
        warnings.append(
            f"ARC_MODEL_RETRY_DELAY={source.get('ARC_MODEL_RETRY_DELAY')!r}; "
            f"effective retry-delay ceiling is {max_delay:g}s (ARC_MODEL_RETRY_MAX_DELAY)."
        )
    return warnings


def resolve_runtime_config(values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize known values using the same aliases as validation."""

    source = _raw_values(values)
    errors = validate_runtime_config(source)
    if errors:
        raise ValueError("Invalid runtime configuration: " + " ".join(errors))
    resolved: dict[str, Any] = {}
    for name, (_kind, _low, _high, default) in _NUMERIC_SPECS.items():
        raw = source.get(name, "")
        if str(raw).strip() == "":
            resolved[name] = default
        else:
            resolved[name] = _parse_number(name, raw, _kind)
    for name, default in _BOOL_DEFAULTS.items():
        text = _text(source.get(name, ""))
        resolved[name] = default if not text else text in TRUE_VALUES
    for name, aliases in _ENUM_ALIASES.items():
        text = _text(source.get(name, ""))
        if not text:
            if name == "ARC_OPENAI_API_MODE":
                resolved[name] = "chat_completions"
            elif name == "ARC_APP_TYPE":
                resolved[name] = "web"
            elif name == "ARC_MODEL_STREAM_TRANSPORT":
                resolved[name] = "stream"
            elif name == "ARC_STRUCTURED_OUTPUT":
                resolved[name] = "auto"
            elif name == "ARC_AGENT_STREAM_VERSION":
                resolved[name] = "v2"
            else:
                resolved[name] = "auto"
        else:
            resolved[name] = aliases[text]
    return resolved

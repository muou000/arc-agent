"""Verify the chat-model client cache in ``agents/model/openai_api_adapter.py``.

ARC builds one agent per stage invocation, so ``build_openai_chat_model`` used to
construct a fresh ChatOpenAI -- and therefore a fresh httpx client -- for every
node, phase and TDD retry. Each new client threw away the previous connection
pool, so every model call re-paid a TCP/TLS handshake. The cache keys on the
client's construction inputs so one pool serves the whole compilation.
"""

from __future__ import annotations

import pytest

from agents.model.openai_api_adapter import (
    build_openai_chat_model,
    reset_model_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _isolated_cache() -> None:
    reset_model_cache_for_tests()
    yield
    reset_model_cache_for_tests()


def _build(**overrides):
    kwargs = {
        "api_mode": "chat_completions",
        "base_url": "https://model.test/v1",
        "api_key": "test-key",
    }
    kwargs.update(overrides)
    return build_openai_chat_model("test-model", **kwargs)


def test_same_configuration_returns_the_same_client() -> None:
    assert _build() is _build()


def test_different_api_key_builds_a_separate_client() -> None:
    assert _build(api_key="key-a") is not _build(api_key="key-b")


def test_different_base_url_builds_a_separate_client() -> None:
    assert _build(base_url="https://a.test/v1") is not _build(base_url="https://b.test/v1")


def test_different_api_mode_builds_a_separate_client() -> None:
    assert _build(api_mode="chat_completions") is not _build(api_mode="responses")


def test_reset_drops_the_cached_client() -> None:
    first = _build()
    reset_model_cache_for_tests()
    assert _build() is not first

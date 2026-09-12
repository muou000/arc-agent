"""Verify the ARC harness profile is registered once per provider key.

``build_stage_agent`` calls ``_register_arc_tool_exclusions`` on every build. The
profile is a constant, so re-registering it for every node, phase and TDD retry
is pure overhead. The registration is now memoised per provider key.
"""

from __future__ import annotations

from agents.runtime import factory


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, key: str, profile: object) -> None:
        self.calls.append(key)


def test_profile_is_registered_once_per_provider_key(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(factory, "register_harness_profile", recorder)
    monkeypatch.setattr(factory, "_REGISTERED_HARNESS_PROFILES", set())

    factory._register_arc_tool_exclusions(model="openai:test-model", resolved_model="test-model")
    first_round = list(recorder.calls)
    assert first_round  # the first build registers something

    factory._register_arc_tool_exclusions(model="openai:test-model", resolved_model="test-model")
    assert recorder.calls == first_round  # the second build is a no-op


def test_a_new_provider_key_is_registered(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(factory, "register_harness_profile", recorder)
    monkeypatch.setattr(factory, "_REGISTERED_HARNESS_PROFILES", {"openai", "openai:model-a"})

    factory._register_arc_tool_exclusions(model="openai:model-b", resolved_model="model-b")

    assert "openai:model-b" in recorder.calls

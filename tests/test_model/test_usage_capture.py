"""Tests for ``agents.model.usage_capture`` — token usage extraction, estimation
fallback, sink dispatch and the ARCChatOpenAI wrapper integration.

Capture must never break a model call: every sink/extraction failure is
contained. These tests also pin the canonical pi-style usage semantics
(``input`` excludes cache tokens, ``reasoning`` is a subset of ``output``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agents.model import openai_api_adapter as adapter
from agents.model import usage_capture
from agents.model.usage_capture import (
    LLMUsageRecord,
    estimate_usage_from_exchange,
    extract_usage_from_chat_result,
    llm_usage_context,
    record_chat_result_usage,
    set_llm_usage_sink,
)


@pytest.fixture(autouse=True)
def _reset_usage_state():
    yield
    set_llm_usage_sink(None)
    with usage_capture._encoder_cache_lock:
        usage_capture._encoder_cache.clear()


def _chat_result(message: AIMessage, llm_output: dict | None = None) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)], llm_output=llm_output or {})


def _record_to_dict(record: LLMUsageRecord) -> dict:
    return record.as_dict()


class TestExtractFromUsageMetadata:
    def test_langchain_metadata_with_cache_and_reasoning(self) -> None:
        result = _chat_result(
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                    "input_token_details": {"cache_read": 20, "cache_creation": 10},
                    "output_token_details": {"reasoning": 5},
                },
            )
        )
        assert extract_usage_from_chat_result(result) == {
            "input": 90,  # 120 prompt - 20 cached - 10 written
            "output": 30,
            "cache_read": 20,
            "cache_write": 10,
            "cache_write_1h": None,
            "reasoning": 5,
            "total": 150,
        }

    def test_missing_details_default_to_zero_and_unknown(self) -> None:
        result = _chat_result(
            AIMessage(
                content="hi",
                usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            )
        )
        usage = extract_usage_from_chat_result(result)
        assert usage == {
            "input": 12,
            "output": 3,
            "cache_read": 0,
            "cache_write": 0,
            "cache_write_1h": None,
            "reasoning": None,
            "total": 15,
        }


class TestExtractFromLLMOutput:
    def test_openai_token_usage_shape(self) -> None:
        result = _chat_result(
            AIMessage(content="ok"),
            llm_output={
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 40},
                    "completion_tokens_details": {"reasoning_tokens": 8},
                }
            },
        )
        usage = extract_usage_from_chat_result(result)
        assert usage is not None
        assert usage["input"] == 60
        assert usage["cache_read"] == 40
        assert usage["reasoning"] == 8
        assert usage["total"] == 120

    def test_deepseek_cache_hit_field_fallback(self) -> None:
        result = _chat_result(
            AIMessage(content="ok"),
            llm_output={
                "token_usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 25,
                }
            },
        )
        usage = extract_usage_from_chat_result(result)
        assert usage is not None
        assert usage["cache_read"] == 25
        assert usage["input"] == 25

    def test_metadata_takes_priority_over_llm_output(self) -> None:
        result = _chat_result(
            AIMessage(
                content="hi",
                usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            ),
            llm_output={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
        )
        usage = extract_usage_from_chat_result(result)
        assert usage is not None
        assert usage["input"] == 7


class TestEstimationFallback:
    def test_no_usage_anywhere_returns_none(self) -> None:
        result = _chat_result(AIMessage(content="hello world"))
        assert extract_usage_from_chat_result(result) is None

    def test_estimate_uses_tokenizer_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeEncoder:
            def encode(self, text: str) -> list[str]:
                return text.split()

        monkeypatch.setattr(usage_capture, "_load_tokenizer", lambda model: _FakeEncoder())
        messages = [{"role": "user", "content": "one two three four"}]
        result = _chat_result(AIMessage(content="five six"))
        usage = estimate_usage_from_exchange(messages, result, model="test-model")
        # 4 input content tokens + the per-message framing overhead, 2 output tokens.
        assert usage["input"] == 4 + usage_capture.PER_MESSAGE_TOKEN_OVERHEAD
        assert usage["output"] == 2
        assert usage["total"] == 6 + usage_capture.PER_MESSAGE_TOKEN_OVERHEAD
        assert usage["reasoning"] is None

    def test_estimate_falls_back_to_char_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(usage_capture, "_load_tokenizer", lambda model: None)
        messages = [{"role": "user", "content": "x" * 40}]
        result = _chat_result(AIMessage(content="y" * 8))
        usage = estimate_usage_from_exchange(messages, result, model="test-model")
        assert usage["input"] == 10 + usage_capture.PER_MESSAGE_TOKEN_OVERHEAD
        assert usage["output"] == 2

    def test_empty_messages_estimate_zero(self) -> None:
        usage = estimate_usage_from_exchange([], _chat_result(AIMessage(content="")), model="")
        assert usage["input"] == 0
        assert usage["output"] == 0


class TestSinkDispatch:
    def test_record_dispatches_to_sink_with_context(self) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        result = _chat_result(
            AIMessage(
                content="hi",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        )
        with llm_usage_context("REQ-1", "DESIGN"):
            record_chat_result_usage(result, model="MiniMax-M3", api_mode="chat_completions")

        assert len(records) == 1
        record = records[0]
        assert record.node_id == "REQ-1"
        assert record.phase == "DESIGN"
        assert record.model == "MiniMax-M3"
        assert record.api_mode == "chat_completions"
        assert record.source == "reported"
        assert record.input_tokens == 10
        # MiniMax-M3 is in the pricing catalog, so a cost breakdown exists.
        assert record.cost is not None and record.cost["total"] > 0

    def test_estimated_source_when_provider_reports_nothing(self) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        result = _chat_result(AIMessage(content="hello"))
        with llm_usage_context("REQ-2", "IMPLEMENT"):
            record_chat_result_usage(
                result,
                model="unpriced-model",
                api_mode="chat_completions",
                messages=[{"role": "user", "content": "question"}],
            )
        assert len(records) == 1
        assert records[0].source == "estimated"
        assert records[0].output_tokens > 0
        assert records[0].cost is None  # unpriced model reports no cost

    def test_no_sink_is_a_noop(self) -> None:
        # Without a sink nothing happens, and no estimation cost is paid.
        record_chat_result_usage(
            _chat_result(AIMessage(content="hi")),
            model="gpt-4o",
            api_mode="chat_completions",
            messages=[{"role": "user", "content": "hi"}],
        )

    def test_sink_errors_are_swallowed(self) -> None:
        def broken_sink(record: LLMUsageRecord) -> None:
            raise RuntimeError("sink exploded")

        set_llm_usage_sink(broken_sink)
        record_chat_result_usage(
            _chat_result(
                AIMessage(
                    content="hi",
                    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            ),
            model="gpt-4o",
            api_mode="chat_completions",
        )

    def test_context_defaults_to_run_level(self) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        record_chat_result_usage(
            _chat_result(
                AIMessage(
                    content="hi",
                    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            ),
            model="gpt-4o",
            api_mode="chat_completions",
        )
        assert records[0].node_id == ""
        assert records[0].phase == ""


class TestAdapterIntegration:
    def _metadata_result(self) -> ChatResult:
        return _chat_result(
            AIMessage(
                content="hi",
                usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            )
        )

    def test_async_generate_reports_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        metadata_result = self._metadata_result()

        async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            return metadata_result

        monkeypatch.setattr(adapter.ChatOpenAI, "_agenerate", fake_agenerate)

        async def run() -> None:
            model = adapter.ARCChatOpenAI(
                model="gpt-4o",
                api_key="test-key",
                arc_api_mode="chat_completions",
                arc_model_name="gpt-4o",
            )
            with llm_usage_context("REQ-9", "IMPLEMENT"):
                await model._agenerate([{"role": "user", "content": "hi"}])

        asyncio.run(run())
        assert len(records) == 1
        assert records[0].model == "gpt-4o"
        assert records[0].api_mode == "chat_completions"
        assert records[0].node_id == "REQ-9"
        assert records[0].input_tokens == 10

    def test_sync_generate_reports_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        metadata_result = self._metadata_result()

        def fake_generate(self, messages, stop=None, run_manager=None, **kwargs):
            return metadata_result

        monkeypatch.setattr(adapter.ChatOpenAI, "_generate", fake_generate)

        model = adapter.ARCChatOpenAI(
            model="gpt-4o",
            api_key="test-key",
            arc_api_mode="chat_completions",
            arc_model_name="gpt-4o",
        )
        with llm_usage_context("REQ-8", "DESIGN"):
            model._generate([{"role": "user", "content": "hi"}])

        assert len(records) == 1
        assert records[0].phase == "DESIGN"

    def test_compatible_model_reports_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.model.compatible_openai import CompatibleChatOpenAI

        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        metadata_result = self._metadata_result()

        async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            return metadata_result

        monkeypatch.setattr(CompatibleChatOpenAI, "_agenerate", fake_agenerate)

        async def run() -> None:
            model = adapter.ARCCompatibleChatOpenAI(
                model="gpt-4o",
                api_key="test-key",
                arc_api_mode="responses",
                arc_model_name="gpt-4o",
            )
            await model._agenerate([{"role": "user", "content": "hi"}])

        asyncio.run(run())
        assert len(records) == 1
        assert records[0].api_mode == "responses"

    def test_failed_model_calls_record_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records: list[LLMUsageRecord] = []
        set_llm_usage_sink(records.append)
        monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")

        async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("not a model api error")

        monkeypatch.setattr(adapter.ChatOpenAI, "_agenerate", fake_agenerate)

        async def run() -> None:
            model = adapter.ARCChatOpenAI(
                model="gpt-4o",
                api_key="test-key",
                arc_api_mode="chat_completions",
                arc_model_name="gpt-4o",
            )
            await model._agenerate([{"role": "user", "content": "hi"}])

        with pytest.raises(RuntimeError):
            asyncio.run(run())
        assert records == []


class TestConfigureRuntimeWiring:
    def test_configure_runtime_persists_usage_events(
        self, tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import service

        service.reset_runtime_for_tests()
        try:
            runtime = service.configure_runtime(project_dir=str(tmp_project_dir))
            result = _chat_result(
                AIMessage(
                    content="hi",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_token_details": {"cache_read": 4},
                    },
                )
            )
            with llm_usage_context("REQ-1", "DESIGN"):
                record_chat_result_usage(
                    result,
                    model="MiniMax-M3",
                    api_mode="chat_completions",
                    messages=[{"role": "user", "content": "hi"}],
                )

            lines = [
                json.loads(line)
                for line in runtime.paths.runner_events_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            assert len(lines) == 1
            event = lines[0]
            assert event["type"] == "llm_usage"
            assert event["node_id"] == "REQ-1"
            assert event["phase"] == "DESIGN"
            assert event["source"] == "reported"
            assert event["usage"]["input"] == 6  # 10 - 4 cached
            assert event["cost"] is not None
        finally:
            service.reset_runtime_for_tests()

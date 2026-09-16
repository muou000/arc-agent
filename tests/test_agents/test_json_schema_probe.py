"""Capability probing for provider-native strict json_schema structured output.

The DESIGN repair path rebuilds its agent with a ``minItems``-constrained
schema only when the endpoint is known to accept chat_completions
``response_format: {type: "json_schema", strict: true}``. These tests pin the
probe's three-state contract (supported / rejected / undeterminable), the
independent cache versus the tool-call probe, and the wiring through
``_dynamic_repair_response_format``.
"""

from __future__ import annotations

import httpx
import pytest

from agents.model import openai_api_adapter
from agents.model.openai_api_adapter import (
    probe_json_schema_support,
    reset_structured_output_support_cache_for_tests,
)
from agents.interface_designer import _dynamic_repair_response_format

_CUSTOM_BASE_URL = "https://proxy.example/v1"
_PROBE_URL = _CUSTOM_BASE_URL + "/chat/completions"

_JSON_SCHEMA_OK_PAYLOAD = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": '{"ok": true}',
            }
        }
    ]
}


class _RecordingTransport(httpx.BaseTransport):
    """Mock transport that records requests and delegates to a responder."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("POST", _PROBE_URL))


def _probe_kwargs(transport: _RecordingTransport) -> dict:
    return {
        "base_url": _CUSTOM_BASE_URL,
        "model": "deepseek-v4-flash",
        "api_mode": "chat_completions",
        "transport": transport,
    }


@pytest.fixture(autouse=True)
def _isolate_probing_env(monkeypatch: pytest.MonkeyPatch):
    for key in ("ARC_STRUCTURED_OUTPUT", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ARC_OPENAI_API_MODE", "MODEL"):
        monkeypatch.delenv(key, raising=False)
    reset_structured_output_support_cache_for_tests()
    yield
    reset_structured_output_support_cache_for_tests()


# ---------------------------------------------------------------------------
# probe_json_schema_support three states
# ---------------------------------------------------------------------------


def test_probe_success_returns_true_and_posts_strict_json_schema() -> None:
    transport = _RecordingTransport(lambda request: _json_response(200, _JSON_SCHEMA_OK_PAYLOAD))

    assert probe_json_schema_support(**_probe_kwargs(transport)) is True

    request = transport.requests[0]
    assert request.url.path.endswith("/chat/completions")
    import json

    payload = json.loads(request.content)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_probe_definitive_rejection_returns_false() -> None:
    transport = _RecordingTransport(
        lambda request: _json_response(400, {"error": {"message": "response_format of type json_schema is not supported"}})
    )

    assert probe_json_schema_support(**_probe_kwargs(transport)) is False


def test_probe_ambiguous_400_is_inconclusive() -> None:
    # A bare 400 (bad model name) says nothing about json_schema support.
    transport = _RecordingTransport(
        lambda request: _json_response(400, {"error": {"message": "invalid model name"}})
    )

    assert probe_json_schema_support(**_probe_kwargs(transport)) is None


def test_probe_auth_and_connection_errors_are_inconclusive() -> None:
    auth_transport = _RecordingTransport(
        lambda request: _json_response(401, {"error": {"message": "bad key"}})
    )
    assert probe_json_schema_support(**_probe_kwargs(auth_transport)) is None

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert probe_json_schema_support(**_probe_kwargs(_RecordingTransport(_raise))) is None


def test_probe_non_json_content_is_false() -> None:
    # A 200 with prose content means the endpoint ignored the schema.
    transport = _RecordingTransport(
        lambda request: _json_response(200, {"choices": [{"message": {"role": "assistant", "content": "Sure, here you go."}}]})
    )

    assert probe_json_schema_support(**_probe_kwargs(transport)) is False


def test_probe_responses_mode_is_not_probed() -> None:
    """The dynamic floor only targets chat_completions; other modes short-circuit."""

    transport = _RecordingTransport(lambda request: _json_response(200, _JSON_SCHEMA_OK_PAYLOAD))

    assert (
        probe_json_schema_support(
            base_url=_CUSTOM_BASE_URL,
            model="deepseek-v4-flash",
            api_mode="responses",
            transport=transport,
        )
        is None
    )
    assert len(transport.requests) == 0


# ---------------------------------------------------------------------------
# json_schema_structured_output_supported resolution and cache
# ---------------------------------------------------------------------------


def test_official_host_is_supported_without_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def _fail(*args, **kwargs):
        raise AssertionError("probe must not run for the official OpenAI host")

    monkeypatch.setattr(openai_api_adapter, "probe_json_schema_support", _fail)

    from agents.model.openai_api_adapter import json_schema_structured_output_supported

    assert json_schema_structured_output_supported("gpt-5.4") is True


def test_custom_endpoint_rejection_disables_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.model.openai_api_adapter import json_schema_structured_output_supported

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(
        lambda request: _json_response(400, {"error": {"message": "response_format type json_schema is unsupported"}})
    )
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_json_schema_support",
        lambda **kwargs: probe_json_schema_support(transport=transport, **kwargs),
    )

    assert json_schema_structured_output_supported("deepseek-v4-flash") is False
    # The decision is cached: one probe per process for this endpoint+model.
    assert json_schema_structured_output_supported("deepseek-v4-flash") is False
    assert len(transport.requests) == 1


def test_custom_endpoint_inconclusive_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.model.openai_api_adapter import json_schema_structured_output_supported

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(
        lambda request: _json_response(401, {"error": {"message": "bad key"}})
    )
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_json_schema_support",
        lambda **kwargs: probe_json_schema_support(transport=transport, **kwargs),
    )

    assert json_schema_structured_output_supported("deepseek-v4-flash") is True


def test_structured_output_off_disables_json_schema_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.model.openai_api_adapter import json_schema_structured_output_supported

    monkeypatch.setenv("ARC_STRUCTURED_OUTPUT", "off")
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)

    def _fail(*args, **kwargs):
        raise AssertionError("ARC_STRUCTURED_OUTPUT=off must decide without probing")

    monkeypatch.setattr(openai_api_adapter, "probe_json_schema_support", _fail)

    assert json_schema_structured_output_supported("deepseek-v4-flash") is False


def test_json_schema_cache_is_independent_of_tool_call_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint may accept tool calling yet reject native json_schema."""

    from agents.model.openai_api_adapter import json_schema_structured_output_supported, structured_output_supported

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)

    def _tool_probe(**kwargs):
        return True

    def _schema_probe(**kwargs):
        return False

    monkeypatch.setattr(openai_api_adapter, "probe_tool_call_support", _tool_probe)
    monkeypatch.setattr(openai_api_adapter, "probe_json_schema_support", _schema_probe)

    assert structured_output_supported("deepseek-v4-flash") is True
    assert json_schema_structured_output_supported("deepseek-v4-flash") is False


# ---------------------------------------------------------------------------
# wiring: _dynamic_repair_response_format
# ---------------------------------------------------------------------------


def test_dynamic_repair_format_none_without_json_schema_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(
        lambda request: _json_response(400, {"error": {"message": "json_schema response_format unsupported"}})
    )
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_json_schema_support",
        lambda **kwargs: probe_json_schema_support(transport=transport, **kwargs),
    )

    assert _dynamic_repair_response_format(3) is None


def test_dynamic_repair_format_carries_min_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(lambda request: _json_response(200, _JSON_SCHEMA_OK_PAYLOAD))
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_json_schema_support",
        lambda **kwargs: probe_json_schema_support(transport=transport, **kwargs),
    )

    fmt = _dynamic_repair_response_format(3)

    assert fmt is not None
    schema = fmt.schema.model_json_schema()
    assert schema["properties"]["interfaces"]["minItems"] == 3
    # The floor is real: fewer rows fail validation.
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        fmt.schema(interfaces=[], summary="s", files_written=[])


def test_dynamic_repair_format_none_for_zero_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(lambda request: _json_response(200, _JSON_SCHEMA_OK_PAYLOAD))
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_json_schema_support",
        lambda **kwargs: probe_json_schema_support(transport=transport, **kwargs),
    )

    assert _dynamic_repair_response_format(0) is None
    assert len(transport.requests) == 0

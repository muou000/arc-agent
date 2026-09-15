"""Capability probing for structured ``response_format`` on custom endpoints.

``factory._resolve_response_format`` used to drop the pydantic response format
for every non-OpenAI ``OPENAI_BASE_URL`` host, which silently disabled the
structured-output channel on OpenAI-compatible proxies. These tests pin the
replacement behaviour: an explicit ``ARC_STRUCTURED_OUTPUT`` override wins,
otherwise the endpoint receives one cached forced-tool-call probe, and the
decision fails open when the probe is inconclusive.
"""

from __future__ import annotations

import httpx
import pytest

from agents.model import openai_api_adapter
from agents.model.openai_api_adapter import (
    probe_tool_call_support,
    reset_structured_output_support_cache_for_tests,
    structured_output_supported,
)
from agents.runtime import factory

_CUSTOM_BASE_URL = "https://proxy.example/v1"
_PROBE_URL = _CUSTOM_BASE_URL + "/chat/completions"

_TOOL_CALL_PAYLOAD = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "arc_capability_ping", "arguments": "{}"},
                    }
                ],
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


def _json_response(status_code: int, payload: object, url: str = _PROBE_URL) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def _isolate_probing_env(monkeypatch: pytest.MonkeyPatch):
    for key in ("ARC_STRUCTURED_OUTPUT", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ARC_OPENAI_API_MODE"):
        monkeypatch.delenv(key, raising=False)
    reset_structured_output_support_cache_for_tests()
    yield
    reset_structured_output_support_cache_for_tests()


def _forbid_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args, **kwargs):
        raise AssertionError("probe must not run in this scenario")

    monkeypatch.setattr(openai_api_adapter, "probe_tool_call_support", _fail)


def test_override_on_keeps_response_format_without_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_STRUCTURED_OUTPUT", "on")
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    _forbid_probe(monkeypatch)

    assert structured_output_supported("deepseek-v4-flash") is True


def test_override_off_disables_without_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_STRUCTURED_OUTPUT", "off")
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    _forbid_probe(monkeypatch)

    assert structured_output_supported("deepseek-v4-flash") is False


def test_invalid_override_falls_back_to_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_STRUCTURED_OUTPUT", "maybe")
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(lambda request: _json_response(200, _TOOL_CALL_PAYLOAD))
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(transport=transport, **kwargs),
    )

    assert structured_output_supported("deepseek-v4-flash") is True
    assert len(transport.requests) == 1


def test_no_base_url_is_supported_without_probe(monkeypatch: pytest.MonkeyPatch):
    _forbid_probe(monkeypatch)

    assert structured_output_supported("deepseek-v4-flash") is True


def test_official_openai_host_skips_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    _forbid_probe(monkeypatch)

    assert structured_output_supported("gpt-5.4") is True


def test_model_object_without_name_is_supported_without_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    _forbid_probe(monkeypatch)

    class _OpaqueModel:
        pass

    assert structured_output_supported(_OpaqueModel()) is True


def test_probe_success_enables_and_caches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    transport = _RecordingTransport(lambda request: _json_response(200, _TOOL_CALL_PAYLOAD))
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(transport=transport, **kwargs),
    )

    assert structured_output_supported("deepseek-v4-flash") is True
    # Second decision must reuse the cached probe result.
    assert structured_output_supported("deepseek-v4-flash") is True
    assert len(transport.requests) == 1

    request = transport.requests[0]
    assert request.url.path.endswith("/chat/completions")
    assert request.headers.get("Authorization") == "Bearer sk-test"


def test_probe_without_tool_call_disables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    payload = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}

    assert probe_tool_call_support(
        base_url=_CUSTOM_BASE_URL,
        model="deepseek-v4-flash",
        api_mode="chat_completions",
        transport=_RecordingTransport(lambda request: _json_response(200, payload)),
    ) is False


def test_probe_tool_rejection_disables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(
        lambda request: _json_response(400, {"error": {"message": "tool_choice is not supported"}})
    )
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(transport=transport, **kwargs),
    )

    assert structured_output_supported("deepseek-v4-flash") is False
    assert len(transport.requests) == 1


def test_probe_ambiguous_400_fails_open(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(
            transport=_RecordingTransport(
                lambda request: _json_response(400, {"error": {"message": "invalid model name"}})
            ),
            **kwargs,
        ),
    )

    # A bare 400 says nothing about tool support: fail open, keep response_format.
    assert structured_output_supported("deepseek-v4-flash") is True


def test_probe_auth_error_fails_open(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(
            transport=_RecordingTransport(lambda request: _json_response(401, {"error": {"message": "bad key"}})),
            **kwargs,
        ),
    )

    assert structured_output_supported("deepseek-v4-flash") is True


def test_probe_connection_error_fails_open(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert probe_tool_call_support(
        base_url=_CUSTOM_BASE_URL,
        model="deepseek-v4-flash",
        api_mode="chat_completions",
        transport=_RecordingTransport(_raise),
    ) is None


def test_probe_non_dict_json_body_is_inconclusive():
    """A 200 whose body is a JSON list/string must fail open, not raise."""

    assert (
        probe_tool_call_support(
            base_url=_CUSTOM_BASE_URL,
            model="deepseek-v4-flash",
            api_mode="chat_completions",
            transport=_RecordingTransport(lambda request: _json_response(200, [1, 2, 3])),
        )
        is None
    )


def test_probe_garbled_error_body_is_inconclusive():
    """A 400 with an undecodable body must fail open, not raise."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json; charset=utf-8"},
            content=b"\xff\xfe\xfa\xfbgarbled-gateway-page",
            request=request,
        )

    assert (
        probe_tool_call_support(
            base_url=_CUSTOM_BASE_URL,
            model="deepseek-v4-flash",
            api_mode="chat_completions",
            transport=_RecordingTransport(responder),
        )
        is None
    )


def test_probe_responses_mode_posts_to_responses_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_OPENAI_API_MODE", "responses")
    seen_urls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return _json_response(
            200,
            {"output": [{"type": "function_call", "name": "arc_capability_ping"}]},
            url=_CUSTOM_BASE_URL + "/responses",
        )

    assert (
        probe_tool_call_support(
            base_url=_CUSTOM_BASE_URL,
            model="deepseek-v4-flash",
            api_mode="responses",
            transport=_RecordingTransport(responder),
        )
        is True
    )
    assert seen_urls[0].endswith("/responses")

    assert (
        probe_tool_call_support(
            base_url=_CUSTOM_BASE_URL,
            model="deepseek-v4-flash",
            api_mode="responses",
            transport=_RecordingTransport(lambda request: _json_response(200, {"output": []}, url=_CUSTOM_BASE_URL + "/responses")),
        )
        is False
    )


def test_cache_reset_allows_reprobe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(
            transport=_RecordingTransport(
                lambda request: _json_response(400, {"error": {"message": "tools are not supported"}})
            ),
            **kwargs,
        ),
    )
    assert structured_output_supported("deepseek-v4-flash") is False

    reset_structured_output_support_cache_for_tests()
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(
            transport=_RecordingTransport(lambda request: _json_response(200, _TOOL_CALL_PAYLOAD)),
            **kwargs,
        ),
    )
    assert structured_output_supported("deepseek-v4-flash") is True


def test_api_key_change_reprobes(monkeypatch: pytest.MonkeyPatch):
    """A probe decision is scoped to the credential that produced it."""

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    transport = _RecordingTransport(lambda request: _json_response(200, _TOOL_CALL_PAYLOAD))
    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(transport=transport, **kwargs),
    )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-key-a")
    assert structured_output_supported("deepseek-v4-flash") is True
    assert structured_output_supported("deepseek-v4-flash") is True
    assert len(transport.requests) == 1

    # A different credential must not reuse the previous decision.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-key-b")
    assert structured_output_supported("deepseek-v4-flash") is True
    assert len(transport.requests) == 2


def test_concurrent_first_call_single_flight(monkeypatch: pytest.MonkeyPatch):
    """Concurrent first-time callers share one probe (per-key single-flight)."""

    import threading

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    probe_started = threading.Event()
    release_probe = threading.Event()
    request_count = {"count": 0}
    count_lock = threading.Lock()

    def slow_responder(request: httpx.Request) -> httpx.Response:
        with count_lock:
            request_count["count"] += 1
        probe_started.set()
        assert release_probe.wait(timeout=10.0)
        return _json_response(200, _TOOL_CALL_PAYLOAD)

    monkeypatch.setattr(
        openai_api_adapter,
        "probe_tool_call_support",
        lambda **kwargs: probe_tool_call_support(transport=_RecordingTransport(slow_responder), **kwargs),
    )

    results: list[bool] = []

    def _caller():
        results.append(structured_output_supported("deepseek-v4-flash"))

    first = threading.Thread(target=_caller)
    first.start()
    assert probe_started.wait(timeout=5.0)

    second = threading.Thread(target=_caller)
    second.start()
    # The second caller must be waiting on the per-key lock, not probing.
    release_probe.set()
    first.join(timeout=10.0)
    second.join(timeout=10.0)

    assert results == [True, True]
    assert request_count["count"] == 1


def test_resolve_response_format_wiring(monkeypatch: pytest.MonkeyPatch):
    class _Format:
        pass

    format_obj = _Format()

    # No response format: decision short-circuits without probing.
    assert factory._resolve_response_format(None, model="deepseek-v4-flash") is None

    monkeypatch.setenv("OPENAI_BASE_URL", _CUSTOM_BASE_URL)
    monkeypatch.setattr(factory, "structured_output_supported", lambda model: True)
    assert factory._resolve_response_format(format_obj, model="deepseek-v4-flash") is format_obj

    monkeypatch.setattr(factory, "structured_output_supported", lambda model: False)
    assert factory._resolve_response_format(format_obj, model="deepseek-v4-flash") is None

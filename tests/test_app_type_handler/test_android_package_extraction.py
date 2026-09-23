"""Android package-name LLM extraction must reach the unified model adapter.

Issue #186: the extraction read ``InterfaceDesigner.client``, an attribute
production never had, so the LLM branch died on AttributeError and every run
silently used the regex fallback. These tests pin the production path: the
call goes through ``create_arc_chat_model`` (faux models pass through as-is,
strings are built by the factory) and every degradation into the regex
fallback logs at warning status.
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage

from tests.helpers.faux import FauxChatModel, faux_text

from app_type_handler.android import AndroidAppType


class _FauxDesigner:
    def __init__(self, model) -> None:
        self.model = model


class _LogCollector:
    def __init__(self) -> None:
        self.records: list[tuple] = []

    def __call__(self, *args):
        self.records.append(args)


def _write_requirements(tmp_path, *, with_resource_ids: bool = True) -> str:
    if with_resource_ids:
        leaf_description = (
            'description: "The new-file button uses `org.billthefarmer.editor:id/newFile`."\n'
        )
    else:
        # No resource-id pattern: the regex fallback finds nothing and lands
        # on the workspace-name package, so fallback results are
        # distinguishable from an LLM-extracted package.
        leaf_description = 'description: "The app shall persist notes in a local database."\n'
    path = tmp_path / "requirements.yaml"
    path.write_text(
        "id: ROOT\n"
        'description: "Editor app."\n'
        "children:\n"
        "  - id: REQ-1\n"
        f"    {leaf_description}",
        encoding="utf-8",
    )
    return str(path)


def _package_response(package_name: str) -> str:
    return json.dumps({"package_name": package_name, "resource_ids": {"newFile": "Button"}})


def test_llm_extraction_reaches_injected_model_and_applies_package(tmp_path) -> None:
    model = FauxChatModel(responses=[faux_text(_package_response("org.billthefarmer.editor"))])
    logs = _LogCollector()
    handler = AndroidAppType(str(tmp_path), _write_requirements(tmp_path), _FauxDesigner(model), logs)

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package == "org.billthefarmer.editor"
    assert model.call_count == 1
    prompt_text = "\n".join(str(message.content) for message in model.calls[0])
    assert "org.billthefarmer.editor:id/newFile" in prompt_text
    assert all(record[2] != "warning" for record in logs.records)


def test_string_model_routes_through_unified_factory(monkeypatch, tmp_path) -> None:
    built = FauxChatModel(responses=[faux_text(_package_response("com.example.reader"))])
    seen_names: list[str] = []

    def fake_build(model_name, **_kwargs):
        seen_names.append(model_name)
        return built

    monkeypatch.setattr("agents.model.factory.build_openai_chat_model", fake_build)
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path), _FauxDesigner("openai:gpt-test"), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package == "com.example.reader"
    assert seen_names == ["gpt-test"]
    assert built.call_count == 1


def test_model_failure_falls_back_with_warning_log(tmp_path) -> None:
    model = FauxChatModel(responses=[RuntimeError("endpoint down")])
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    assert package != "org.billthefarmer.editor"
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("Package extraction" in str(record[1]) for record in warnings)


def test_unparseable_response_falls_back_with_warning_log(tmp_path) -> None:
    model = FauxChatModel(responses=[faux_text("I could not identify any package here.")])
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("no JSON" in str(record[1]) for record in warnings)


def test_unusable_package_name_falls_back_with_warning_log(tmp_path) -> None:
    model = FauxChatModel(responses=[faux_text(_package_response("UNKNOWN"))])
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("Package extraction" in str(record[1]) for record in warnings)


def test_malformed_package_segments_fall_back_with_warning_log(tmp_path) -> None:
    model = FauxChatModel(responses=[faux_text(_package_response("1editor.app"))])
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("malformed package name" in str(record[1]) for record in warnings)


def test_segmented_content_list_response_is_read(tmp_path) -> None:
    """Multimodal content lists (dict parts) must not become a spurious fallback."""

    model = FauxChatModel(
        responses=[AIMessage(content=[{"type": "text", "text": _package_response("com.example.reader")}])]
    )
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package == "com.example.reader"
    assert all(record[2] != "warning" for record in logs.records)


def test_non_string_package_name_falls_back_with_warning_log(tmp_path) -> None:
    """A numeric package_name must not surface as a bogus 'LLM call failed' error."""

    model = FauxChatModel(responses=[faux_text('{"package_name": 123, "resource_ids": {}}')])
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("no usable package name" in str(record[1]) for record in warnings)
    assert not any("via LLM failed" in str(record[1]) for record in logs.records)

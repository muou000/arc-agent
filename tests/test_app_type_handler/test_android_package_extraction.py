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
from types import SimpleNamespace

from app_type_handler.android import AndroidAppType


class _FauxModel:
    """Stands in for an adapter-built chat model; records ``ainvoke`` calls."""

    def __init__(self, *, content: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[list] = []
        self._content = content
        self._error = error

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content)


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


def _extraction_logs(logs: _LogCollector) -> list[tuple]:
    return [record for record in logs.records if "Package extraction" in str(record[1]) or "no JSON" in str(record[1])]


def test_llm_extraction_reaches_injected_model_and_applies_package(tmp_path) -> None:
    payload = json.dumps(
        {"package_name": "org.billthefarmer.editor", "resource_ids": {"newFile": "Button"}}
    )
    model = _FauxModel(content=payload)
    logs = _LogCollector()
    handler = AndroidAppType(str(tmp_path), _write_requirements(tmp_path), _FauxDesigner(model), logs)

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package == "org.billthefarmer.editor"
    assert len(model.calls) == 1
    prompt_text = "\n".join(
        str(getattr(message, "content", message)) for message in model.calls[0]
    )
    assert "org.billthefarmer.editor:id/newFile" in prompt_text
    assert not _extraction_logs(logs) or all(
        record[2] != "warning" for record in _extraction_logs(logs)
    )


def test_string_model_routes_through_unified_factory(monkeypatch, tmp_path) -> None:
    built = _FauxModel(
        content=json.dumps({"package_name": "com.example.reader", "resource_ids": {}})
    )
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
    assert len(built.calls) == 1


def test_model_failure_falls_back_with_warning_log(tmp_path) -> None:
    model = _FauxModel(error=RuntimeError("endpoint down"))
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
    model = _FauxModel(content="I could not identify any package here.")
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("no JSON" in str(record[1]) for record in warnings)


def test_unusable_package_name_falls_back_with_warning_log(tmp_path) -> None:
    model = _FauxModel(content=json.dumps({"package_name": "UNKNOWN", "resource_ids": {}}))
    logs = _LogCollector()
    handler = AndroidAppType(
        str(tmp_path), _write_requirements(tmp_path, with_resource_ids=False), _FauxDesigner(model), logs
    )

    package = asyncio.run(handler._extract_android_package_name_via_llm())

    assert package.startswith("com.")
    warnings = [record for record in logs.records if record[2] == "warning"]
    assert any("Package extraction" in str(record[1]) for record in warnings)

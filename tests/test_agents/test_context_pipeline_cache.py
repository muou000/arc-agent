"""Regression tests for the context pipeline's caching behaviour.

Stage agents call ``ContextPipeline.configure()`` at the start of every run.
That call used to clear the whole node context cache, so nothing memoised ever
survived a second run: every TDD retry re-read the node session files and
re-scanned the traceability tables. These tests pin the corrected behaviour:
``configure()`` only drops the cache when a setting really changed, and the
interfaces table is indexed once per table revision instead of re-read per node.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.context.pipeline import ContextPipeline


@pytest.fixture
def pipeline(arc_runtime) -> ContextPipeline:
    """A pipeline bound to the temporary runtime, isolated from the singleton."""

    instance = ContextPipeline()
    instance.set_runtime(arc_runtime)
    return instance


def test_repeated_configure_keeps_the_cache_warm(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    workspace = str(tmp_project_dir.resolve())
    # Settle the configuration first, then warm the cache.
    pipeline.configure(workspace_dir=workspace, app_type="web")
    computed: list[int] = []
    pipeline.cache.get_or_compute("REQ-1", "probe", lambda: computed.append(1) or "value")

    # A second and third run repeat the exact same settings.
    pipeline.configure(workspace_dir=workspace, app_type="web")
    pipeline.configure(workspace_dir=workspace, app_type="web")

    assert pipeline.cache.get_or_compute("REQ-1", "probe", lambda: computed.append(1) or "other") == "value"
    assert computed == [1]


def test_configure_with_a_new_setting_invalidates_the_cache(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    computed: list[int] = []
    pipeline.cache.get_or_compute("REQ-1", "probe", lambda: computed.append(1) or "value")

    pipeline.configure(app_type="android")

    assert pipeline.cache.get_or_compute("REQ-1", "probe", lambda: computed.append(1) or "other") == "other"
    assert computed == [1, 1]


def test_interface_index_is_reused_until_the_table_changes(
    pipeline: ContextPipeline,
    arc_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = arc_runtime.traceability
    store.upsert_requirement(req_id="REQ-1", name="Root")
    store.upsert_requirement(req_id="REQ-1.1", name="Child", parent_id="REQ-1")
    store.upsert_interface(
        interface_id="IF-1",
        req_ids=["REQ-1.1"],
        type="ui_component",
        content='{"responsibility":"child"}',
    )
    store.upsert_interface(
        interface_id="IF-2",
        req_ids=["REQ-1"],
        type="ui_component",
        content='{"responsibility":"parent"}',
    )

    reads: list[int] = []
    original = store.list_interfaces

    def counting_list_interfaces(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "list_interfaces", counting_list_interfaces)

    first = pipeline._get_existing_interface_cards("REQ-1.1")
    second = pipeline._get_existing_interface_cards("REQ-1.1")

    assert len(reads) == 1, "the interfaces table should be indexed once, not re-read per node"
    assert first == second
    assert "IF-1" in first and "IF-2" in first

    # A new interface written by another node must invalidate the index.
    store.upsert_interface(
        interface_id="IF-3",
        req_ids=["REQ-1.1"],
        type="service",
        content='{"responsibility":"newly designed"}',
    )
    third = pipeline._get_existing_interface_cards("REQ-1.1")

    assert len(reads) == 2
    assert "IF-3" in third


def test_existing_interface_cards_classify_relations(
    pipeline: ContextPipeline,
    arc_runtime,
) -> None:
    store = arc_runtime.traceability
    store.upsert_requirement(req_id="REQ-1", name="Root")
    store.upsert_requirement(
        req_id="REQ-1.1",
        name="Child",
        parent_id="REQ-1",
        dependencies=["REQ-1.2"],
    )
    store.upsert_requirement(req_id="REQ-1.2", name="Dependency")
    store.upsert_requirement(req_id="REQ-1.3", name="Unrelated")
    for interface_id, req_id in (
        ("IF-CUR", "REQ-1.1"),
        ("IF-PAR", "REQ-1"),
        ("IF-DEP", "REQ-1.2"),
        ("IF-OTH", "REQ-1.3"),
    ):
        store.upsert_interface(
            interface_id=interface_id,
            req_ids=[req_id],
            type="ui_component",
            content="{}",
        )

    cards = pipeline._get_existing_interface_cards("REQ-1.1")

    assert '"relation":"current"' in cards
    assert '"relation":"parent"' in cards
    assert '"relation":"dependency"' in cards
    assert '"relation":"existing"' in cards

"""Regression tests for the context pipeline's caching behaviour.

Stage agents call ``ContextPipeline.configure()`` at the start of every run.
That call used to clear the whole node context cache, so nothing memoised ever
survived a second run: every TDD retry re-read the node session files and
re-scanned the traceability tables. These tests pin the corrected behaviour:
``configure()`` only drops the cache when a setting really changed, and the
interfaces table is indexed once per table revision instead of re-read per node.

The scaffold-file layer is pinned here too: its content is shared by every
node, so it is read once and memoised against a per-file fingerprint until a
node edits one of the scaffold files.
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


def _write_web_scaffolds(workspace: Path) -> None:
    database = workspace / "backend" / "src" / "database"
    database.mkdir(parents=True, exist_ok=True)
    (database / "test_harness.js").write_text(
        "module.exports = { allocateTestDb() { return 'db'; } };\n", encoding="utf-8"
    )
    (database / "init_db.js").write_text(
        "module.exports = { initializeDatabase() {} };\n", encoding="utf-8"
    )
    frontend = workspace / "frontend" / "test"
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "setup.ts").write_text("import '@testing-library/jest-dom';\n", encoding="utf-8")


def test_scaffold_files_are_read_once_until_a_node_edits_one(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_web_scaffolds(tmp_project_dir)
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")

    reads: list[str] = []
    original = ContextPipeline._read_scaffold_file

    def counting_read(path: Path) -> str | None:
        reads.append(str(path))
        return original(path)

    monkeypatch.setattr(ContextPipeline, "_read_scaffold_file", staticmethod(counting_read))

    first = pipeline._get_scaffold_files_context()
    assert "<scaffold_files>" in first
    assert "allocateTestDb" in first
    reads_after_first = len(reads)
    assert reads_after_first == 3, "each declared scaffold file is read exactly once"

    second = pipeline._get_scaffold_files_context()
    assert second == first
    assert len(reads) == reads_after_first, "the block is memoised across nodes until a file changes"

    # A node edits a scaffold file (e.g. registers more tables in init_db.js);
    # the fingerprint must invalidate the block for the next node.
    (tmp_project_dir / "backend" / "src" / "database" / "init_db.js").write_text(
        "module.exports = { initializeDatabase() { /* v2: more tables */ } };\n",
        encoding="utf-8",
    )
    third = pipeline._get_scaffold_files_context()

    assert "v2: more tables" in third
    assert len(reads) > reads_after_first


def test_scaffold_layer_is_gated_to_stage_agents_and_stays_static_in_the_split(
    pipeline: ContextPipeline,
    arc_runtime,
    tmp_project_dir: Path,
) -> None:
    _write_web_scaffolds(tmp_project_dir)
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    store = arc_runtime.traceability
    store.upsert_requirement(req_id="REQ-1", name="Root")

    assert "<scaffold_files>" in pipeline.get_static_context("REQ-1", "InterfaceDesigner")
    assert "<scaffold_files>" in pipeline.get_static_context("REQ-1", "TestDrivenDeveloper")
    assert "<scaffold_files>" not in pipeline.get_static_context("REQ-1", "")

    static, dynamic = pipeline.build_agent_context_split(node_id="REQ-1", agent_type="TestGenerator")
    assert "<scaffold_files>" in static
    assert "<scaffold_files>" not in dynamic, "scaffold content is static context, not per-node dynamic context"


def test_scaffold_layer_is_empty_without_scaffold_files(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")

    assert pipeline._get_scaffold_files_context() == ""
    assert "<scaffold_files>" not in pipeline.get_static_context("REQ-1", "InterfaceDesigner")


def test_scaffold_layer_truncates_oversized_files(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    config_dir = tmp_project_dir / "backend"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "vitest.config.js").write_text("// " + "x" * 6000 + "\n", encoding="utf-8")
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")

    block = pipeline._get_scaffold_files_context()

    assert "... [truncated]" in block
    assert len(block) < 6000


def test_scaffold_cache_resets_when_the_app_type_changes(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    _write_web_scaffolds(tmp_project_dir)
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    assert "<scaffold_files>" in pipeline._get_scaffold_files_context()

    pipeline.configure(app_type="cli")

    assert pipeline._get_scaffold_files_context() == "", "cli declares no scaffold files"


def test_scaffold_layer_respects_the_total_budget(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    database = tmp_project_dir / "backend" / "src" / "database"
    database.mkdir(parents=True, exist_ok=True)
    (database / "init_db.js").write_text("A" * 400 + "\n", encoding="utf-8")
    (database / "db_runtime.js").write_text("B" * 400 + "\n", encoding="utf-8")
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    pipeline.SCAFFOLD_TOTAL_CHAR_LIMIT = 500

    block = pipeline._get_scaffold_files_context()

    assert "init_db.js" in block
    assert "db_runtime.js" not in block, "a file that would exceed the budget is excluded, not appended oversized"


def test_scaffold_layer_always_includes_the_first_file(
    pipeline: ContextPipeline,
    tmp_project_dir: Path,
) -> None:
    database = tmp_project_dir / "backend" / "src" / "database"
    database.mkdir(parents=True, exist_ok=True)
    (database / "init_db.js").write_text("A" * 900 + "\n", encoding="utf-8")
    pipeline.configure(workspace_dir=str(tmp_project_dir.resolve()), app_type="web")
    pipeline.SCAFFOLD_TOTAL_CHAR_LIMIT = 100

    block = pipeline._get_scaffold_files_context()

    assert "init_db.js" in block, "the budget must never collapse the layer into an empty block"

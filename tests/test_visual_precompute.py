"""Reference-image analysis must be precomputed in parallel, not per node.

DESIGN used to block each image-bearing node on a serial vision call (~1.5-2.5
minutes per image; 34 image nodes on the 12306 benchmark). The persisted
analysis and per-image cache let a single concurrent precompute pass turn every
design phase into a cache hit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import visual_analysis


@pytest.fixture()
def image_env(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    requirements = tmp_path / "input" / "requirements"
    reference = requirements / "reference"
    reference.mkdir(parents=True)
    (workspace / ".arc").mkdir(parents=True)
    (reference / "a.png").write_bytes(b"\x89PNG a")
    (reference / "b.png").write_bytes(b"\x89PNG b")
    return workspace, requirements


def _node(req_id: str, image_name: str, *, analysis: str = "") -> tuple[str, dict]:
    data = {
        "id": req_id,
        "description": f"![shot](./reference/{image_name})",
        "visual_reference": [],
    }
    if analysis:
        data["visual_reference"] = [{"image_path": f"./reference/{image_name}", "analysis": analysis}]
    return req_id, data


def _stub_runtime(updates: list):
    traceability = SimpleNamespace(update_requirement_fields=lambda req_id, **fields: updates.append((req_id, fields)))
    return SimpleNamespace(traceability=traceability)


def test_no_images_means_no_api_call(image_env, monkeypatch) -> None:
    workspace, requirements = image_env

    def fail_request(full_path):
        raise AssertionError("the vision API must not be called without images")

    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", fail_request)
    count = asyncio.run(
        visual_analysis.precompute_visual_references(
            workspace_path=str(workspace),
            requirements_dir=str(requirements),
            requirement_nodes=[("REQ-9", {"id": "REQ-9", "description": "text only"})],
        )
    )
    assert count == 0


def test_pending_images_are_analyzed_concurrently(image_env, monkeypatch) -> None:
    workspace, requirements = image_env
    in_flight = 0
    max_in_flight = 0
    calls: list[str] = []

    async def fake_request(full_path):
        nonlocal in_flight, max_in_flight
        calls.append(Path(full_path).name)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return f"analysis for {Path(full_path).name}"

    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", fake_request)
    updates: list = []
    monkeypatch.setattr(visual_analysis, "get_runtime", lambda: _stub_runtime(updates))

    count = asyncio.run(
        visual_analysis.precompute_visual_references(
            workspace_path=str(workspace),
            requirements_dir=str(requirements),
            requirement_nodes=[_node("REQ-1", "a.png"), _node("REQ-2", "b.png")],
        )
    )

    assert count == 2
    assert max_in_flight == 2, "independent image analyses must overlap"
    assert sorted(calls) == ["a.png", "b.png"]
    updated_ids = {req_id for req_id, _fields in updates}
    assert updated_ids == {"REQ-1", "REQ-2"}


def test_nodes_with_existing_analysis_are_skipped(image_env, monkeypatch) -> None:
    workspace, requirements = image_env

    def fail_request(full_path):
        raise AssertionError("already-analyzed images must not hit the API again")

    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", fail_request)
    count = asyncio.run(
        visual_analysis.precompute_visual_references(
            workspace_path=str(workspace),
            requirements_dir=str(requirements),
            requirement_nodes=[_node("REQ-1", "a.png", analysis="already done")],
        )
    )
    assert count == 0


def test_concurrent_tasks_do_not_erase_sibling_cache_entries(
    image_env,
    monkeypatch,
) -> None:
    """Each task loads the cache file before its vision calls; a plain
    whole-file save at the end would overwrite the entries its siblings saved
    in the meantime, costing duplicate vision API calls on the next run."""
    workspace, requirements = image_env

    async def fake_request(full_path: str) -> str:
        await asyncio.sleep(0.05)
        return f"analysis for {Path(full_path).name}"

    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", fake_request)
    updates: list = []
    monkeypatch.setattr(visual_analysis, "get_runtime", lambda: _stub_runtime(updates))

    asyncio.run(
        visual_analysis.precompute_visual_references(
            workspace_path=str(workspace),
            requirements_dir=str(requirements),
            requirement_nodes=[_node("REQ-1", "a.png"), _node("REQ-2", "b.png")],
        )
    )

    import json

    cache = json.loads((workspace / ".arc" / "visual_analysis_cache.json").read_text(encoding="utf-8"))
    assert len(cache) == 2, f"both image entries must survive the concurrent pass, got: {sorted(cache)}"


def test_one_failing_node_does_not_abort_the_precompute_pass(
    image_env,
    monkeypatch,
) -> None:
    """A persist failure for one node (e.g. a requirement missing from the
    traceability store) must degrade to a per-node error, not abort the whole
    compile before the queue drains."""
    workspace, requirements = image_env
    logs: list[tuple] = []
    updates: list = []

    class _PartiallyFailingRuntime:
        def __init__(self) -> None:
            self.traceability = SimpleNamespace(update_requirement_fields=self._update)

        def _update(self, req_id: str, **fields) -> None:
            if req_id == "REQ-1":
                raise ValueError(f"Requirement not found: {req_id}")
            updates.append((req_id, fields))

    async def log_cb(agent_name, message, status=None, node_id=None):
        logs.append((node_id, status))

    async def fake_request(full_path: str) -> str:
        await asyncio.sleep(0.01)
        return f"analysis for {Path(full_path).name}"

    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", fake_request)
    monkeypatch.setattr(visual_analysis, "get_runtime", lambda: _PartiallyFailingRuntime())

    count = asyncio.run(
        visual_analysis.precompute_visual_references(
            workspace_path=str(workspace),
            requirements_dir=str(requirements),
            requirement_nodes=[_node("REQ-1", "a.png"), _node("REQ-2", "b.png")],
            log_cb=log_cb,
        )
    )

    assert count == 1, "only the healthy node counts as analyzed"
    assert {req_id for req_id, _fields in updates} == {"REQ-2"}
    assert ("REQ-1", "error") in logs


def test_precompute_env_toggle() -> None:
    import os

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.delenv("ARC_VISUAL_PRECOMPUTE", raising=False)
        assert visual_analysis.visual_precompute_enabled() is True
        monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "0")
        assert visual_analysis.visual_precompute_enabled() is False
    finally:
        monkeypatch.undo()

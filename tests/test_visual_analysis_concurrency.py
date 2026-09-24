"""Independent visual analyses must fan out instead of running one by one."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import visual_analysis
from core.service import configure_runtime, get_runtime, reset_runtime_for_tests


@pytest.fixture
def visual_workspace(tmp_path: Path) -> dict[str, Any]:
    """Workspace with a published runtime whose traceability store knows req-1.

    ``analyze_and_attach_visual_references`` stores its results through the
    process-wide runtime, so every test that expects stored references needs
    the runtime bound to the same directory it passes as ``workspace_path``.
    """

    workspace = tmp_path / "project"
    requirements_dir = workspace / "requirements"
    requirements_dir.mkdir(parents=True)
    runtime = configure_runtime(project_dir=str(workspace))
    runtime.traceability.init_store(reset=True)
    runtime.traceability.upsert_requirement(req_id="req-1", name="Req 1", description="desc")
    yield {"workspace": workspace, "requirements_dir": requirements_dir}
    reset_runtime_for_tests()


def _write_image(requirements_dir: Path, name: str) -> None:
    image = requirements_dir / name
    image.write_bytes(b"png")


def _requirement_data(images: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "req-1",
        "description": "",
        "visual_reference": list(images),
    }


class _ConcurrencyRecorder:
    """Fake analysis that tracks how many requests were in flight at once."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.in_flight = 0
        self.peak = 0
        self.requested: list[str] = []

    async def __call__(self, full_path: Path) -> str:
        self.requested.append(full_path.name)
        failure = self.failures.get(full_path.name)
        if failure is not None:
            raise failure
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            self.in_flight -= 1
        return f"analysis:{full_path.name}"


async def _analyze(
    visual_workspace: dict[str, Any],
    images: list[dict[str, Any]],
    log_cb: Any = None,
) -> dict[str, Any]:
    return await visual_analysis.analyze_and_attach_visual_references(
        workspace_path=str(visual_workspace["workspace"]),
        requirements_dir=str(visual_workspace["requirements_dir"]),
        requirement_data=_requirement_data(images),
        log_cb=log_cb,
    )


def test_visual_analyses_fan_out_by_default(
    visual_workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARC_VISUAL_ANALYSIS_CONCURRENCY", raising=False)
    names = ["a.png", "b.png", "c.png", "d.png"]
    for name in names:
        _write_image(visual_workspace["requirements_dir"], name)
    recorder = _ConcurrencyRecorder()
    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", recorder)
    logs: list[tuple[str, str | None]] = []

    result = asyncio.run(
        _analyze(
            visual_workspace,
            [{"image_path": name} for name in names],
            log_cb=lambda agent, message, status=None, node_id=None: logs.append((message, status)),
        )
    )

    assert recorder.peak == len(names)
    assert sorted(recorder.requested) == sorted(names)
    references = result["visual_reference"]
    assert [payload["image_path"] for payload in references] == names
    assert [payload["analysis"] for payload in references] == [f"analysis:{name}" for name in names]
    # The persist path (#214) must keep the dict payloads: the requirement row
    # read back from the store still carries image_path/analysis entries, not
    # their str() reprs.
    stored = get_runtime().traceability.get_requirement("req-1") or {}
    assert stored["visual_reference"] == [
        {"image_path": name, "analysis": f"analysis:{name}", "resolved_image_path": str(visual_workspace["requirements_dir"] / name)}
        for name in names
    ]
    cache = visual_analysis._load_visual_cache(str(visual_workspace["workspace"]))
    assert len(cache) == len(names)
    assert not [status for _, status in logs if status == "error"]


def test_visual_analyses_run_serially_when_concurrency_is_one(
    visual_workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["a.png", "b.png", "c.png"]
    for name in names:
        _write_image(visual_workspace["requirements_dir"], name)
    monkeypatch.setenv("ARC_VISUAL_ANALYSIS_CONCURRENCY", "1")
    recorder = _ConcurrencyRecorder()
    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", recorder)

    asyncio.run(_analyze(visual_workspace, [{"image_path": name} for name in names]))

    assert recorder.peak == 1


def test_visual_analysis_preserves_order_and_isolates_failures(
    visual_workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    requirements_dir: Path = visual_workspace["requirements_dir"]
    for name in ("kept.png", "cached.png", "fresh.png", "boom.png"):
        _write_image(requirements_dir, name)
    # missing.png stays absent so it is skipped before any request.
    monkeypatch.setenv("VISUAL_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISUAL_API_KEY", "key")
    cached_image = requirements_dir / "cached.png"
    cache_key = visual_analysis._build_visual_cache_key(cached_image)
    visual_analysis._save_visual_cache(
        str(visual_workspace["workspace"]),
        {cache_key: {"image_path": "cached.png", "analysis": "cached analysis"}},
    )
    recorder = _ConcurrencyRecorder(failures={"boom.png": RuntimeError("provider down")})
    monkeypatch.setattr(visual_analysis, "_request_visual_analysis", recorder)
    logs: list[tuple[str, str | None]] = []

    result = asyncio.run(
        _analyze(
            visual_workspace,
            [
                {"image_path": "kept.png", "analysis": "pre-existing analysis"},
                {"image_path": "cached.png"},
                {"image_path": "missing.png"},
                {"image_path": "boom.png"},
                {"image_path": "fresh.png"},
            ],
            log_cb=lambda agent, message, status=None, node_id=None: logs.append((message, status)),
        )
    )

    references = result["visual_reference"]
    assert [payload["image_path"] for payload in references] == ["kept.png", "cached.png", "fresh.png"]
    assert references[0]["analysis"] == "pre-existing analysis"
    assert references[1]["analysis"] == "cached analysis"
    assert references[2]["analysis"] == "analysis:fresh.png"
    assert recorder.requested == ["boom.png", "fresh.png"]
    messages = " | ".join(message for message, _ in logs)
    assert "provider down" in messages
    assert "Image not found" in messages
    assert "Reusing cached visual analysis: cached.png" in messages
    stored_cache = visual_analysis._load_visual_cache(str(visual_workspace["workspace"]))
    # Only the fresh analysis was added; the seeded entry is untouched.
    assert len(stored_cache) == 2
    fresh_image = requirements_dir / "fresh.png"
    assert stored_cache[visual_analysis._build_visual_cache_key(fresh_image)]["analysis"] == "analysis:fresh.png"


def test_visual_client_is_reused_across_concurrent_requests(
    visual_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    names = ["a.png", "b.png", "c.png"]
    for name in names:
        _write_image(visual_workspace["requirements_dir"], name)
    monkeypatch.setenv("VISUAL_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISUAL_API_KEY", "key")
    visual_analysis.reset_visual_client_cache_for_tests()
    request.addfinalizer(visual_analysis.reset_visual_client_cache_for_tests)

    lock = threading.Lock()
    create_calls = {"count": 0}
    built_clients: list[Any] = []

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            with lock:
                create_calls["count"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="analysis:ok"))])

    class FakeOpenAI:
        def __init__(self, api_key: str, base_url: str, **kwargs: Any) -> None:
            self.api_key = api_key
            self.base_url = base_url
            self.chat = SimpleNamespace(completions=FakeCompletions())
            built_clients.append(self)

    monkeypatch.setattr(visual_analysis, "OpenAI", FakeOpenAI)

    result = asyncio.run(_analyze(visual_workspace, [{"image_path": name} for name in names]))

    assert len(built_clients) == 1
    assert create_calls["count"] == len(names)
    assert [payload["analysis"] for payload in result["visual_reference"]] == ["analysis:ok"] * len(names)


def test_concurrency_knob_is_parsed_and_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_VISUAL_ANALYSIS_CONCURRENCY", raising=False)
    assert visual_analysis._max_visual_analysis_concurrency() == visual_analysis.DEFAULT_VISUAL_ANALYSIS_CONCURRENCY

    cases = {
        "1": 1,
        "0": 1,
        "-3": 1,
        "99": visual_analysis.MAX_VISUAL_ANALYSIS_CONCURRENCY,
        "not-a-number": visual_analysis.DEFAULT_VISUAL_ANALYSIS_CONCURRENCY,
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("ARC_VISUAL_ANALYSIS_CONCURRENCY", raw)
        assert visual_analysis._max_visual_analysis_concurrency() == expected


def test_visual_client_sets_explicit_timeout_and_no_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw visual client must carry the same contract as the model
    adapter: an explicit timeout and no hidden SDK retries, so a dropped
    connection fails once instead of stretching to 600s x 3 attempts."""

    import httpx

    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(visual_analysis, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(visual_analysis, "resolve_model_request_timeout", lambda: httpx.Timeout(120, connect=5))
    monkeypatch.setenv("VISUAL_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISUAL_API_KEY", "key")
    try:
        visual_analysis._get_visual_client()
    finally:
        visual_analysis.reset_visual_client_cache_for_tests()

    assert captured["max_retries"] == 0
    timeout = captured["timeout"]
    assert timeout.read == 120
    assert timeout.connect == 5

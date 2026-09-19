"""The E2E frontend build must be skipped when nothing changed.

Every E2E attempt used to run `npm run build` unconditionally, so a node whose
E2E layer failed and retried paid a full Vite build per attempt even though the
frontend sources were identical. The build is now reused while the source tree
is byte-for-byte unchanged, and rebuilt as soon as it is not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app_type_handler import web as web_handler


class _BuildRecorder:
    """Stands in for `npm run build`, mimicking a real Vite build."""

    def __init__(self, exit_code: int = 0, produce_dist: bool = True) -> None:
        self.exit_code = exit_code
        self.produce_dist = produce_dist
        self.calls: list[str] = []

    async def __call__(
        self,
        command: str,
        cwd: str,
        timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(command)
        if self.produce_dist:
            dist_dir = Path(cwd) / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
        return f"Exit Code: {self.exit_code}\nSTDOUT:\nfake build\n"


def _make_workspace(tmp_path: Path) -> Path:
    """Create a project root containing a minimal frontend and return the root."""

    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "main.js").write_text("console.log('v1')\n", encoding="utf-8")
    (frontend / "package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")
    return tmp_path


def _try_symlink_to(link: Path, target: Path) -> None:
    """Create a directory symlink or skip the test if the host cannot provide one.

    Creating the link is not enough: some sandboxes accept ``symlink_to`` without
    raising yet silently drop the entry, leaving ``link`` non-existent. Asserting
    on the result instead of on the absence of an exception keeps the test honest
    - otherwise it runs against a missing directory and fails for the wrong
    reason.
    """

    import pytest

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require elevated privileges on this host")

    if not link.is_symlink() or not link.exists():
        pytest.skip("this host silently drops directory symlinks")


def _build(workspace_root: Path) -> tuple[bool, str]:
    return asyncio.run(web_handler._build_frontend_dist(str(workspace_root)))


def test_reuses_dist_when_sources_are_unchanged(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    first_ok, first_output = _build(workspace)
    second_ok, second_output = _build(workspace)

    assert first_ok and second_ok
    assert recorder.calls == ["npm run build"]
    assert "fake build" in first_output
    assert "Reused the existing" in second_output


def test_rebuilds_when_a_source_file_changes(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    _build(workspace)
    (workspace / "frontend" / "src" / "main.js").write_text("console.log('v2')\n", encoding="utf-8")
    _build(workspace)

    assert len(recorder.calls) == 2


def test_rebuild_output_states_the_verdict_with_source_fingerprint(tmp_path, monkeypatch) -> None:
    """A fresh build must name what it served, like the reuse path does.

    `dist/` is deny-listed for reads, so the run result's build verdict is the
    only way a TDD session can know whether the backend serves the current
    sources; the failure digest parses this exact line.
    """

    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    fingerprint_v1 = web_handler._frontend_source_fingerprint(str(workspace / "frontend"))
    first_ok, first_output = _build(workspace)
    (workspace / "frontend" / "src" / "main.js").write_text("console.log('v2')\n", encoding="utf-8")
    fingerprint_v2 = web_handler._frontend_source_fingerprint(str(workspace / "frontend"))
    second_ok, second_output = _build(workspace)

    assert first_ok and second_ok
    assert (
        f"Built `frontend/dist` from the current sources (fingerprint {fingerprint_v1[:12]})"
        in first_output
    )
    assert (
        f"Built `frontend/dist` from the current sources (fingerprint {fingerprint_v2[:12]})"
        in second_output
    )


def test_rebuilds_when_a_dist_artifact_changes(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    _build(workspace)
    (workspace / "frontend" / "dist" / "index.html").write_text(
        "<html>partial rebuild</html>\n",
        encoding="utf-8",
    )
    ok, output = _build(workspace)

    assert ok is True
    assert recorder.calls == ["npm run build", "npm run build"]
    assert "Reused the existing" not in output


def test_rebuilds_when_the_built_dist_disappears(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    _build(workspace)
    dist_dir = workspace / "frontend" / "dist"
    for entry in dist_dir.iterdir():
        entry.unlink()
    dist_dir.rmdir()

    _build(workspace)

    assert len(recorder.calls) == 2


def test_a_failed_build_records_no_fingerprint(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder(exit_code=1, produce_dist=False)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    ok, _ = _build(workspace)
    assert ok is False
    assert web_handler._read_recorded_frontend_fingerprint(str(workspace / "frontend")) is None

    _build(workspace)
    assert len(recorder.calls) == 2


def test_a_failed_rebuild_clears_the_previous_fingerprint(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    _build(workspace)
    (workspace / "frontend" / "dist" / "index.html").write_text("partial\n", encoding="utf-8")
    recorder.exit_code = 1
    recorder.produce_dist = False

    ok, _ = _build(workspace)

    assert ok is False
    assert web_handler._read_recorded_frontend_fingerprint(str(workspace / "frontend")) is None


def test_a_stale_dist_without_a_recorded_fingerprint_is_not_reused(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    dist_dir = workspace / "frontend" / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>stale</html>\n", encoding="utf-8")

    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    ok, _ = _build(workspace)

    assert ok is True
    assert recorder.calls == ["npm run build"]


def test_a_legacy_source_only_fingerprint_is_not_reused(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    frontend = workspace / "frontend"
    dist_dir = frontend / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>legacy</html>\n", encoding="utf-8")
    source_fingerprint = web_handler._frontend_source_fingerprint(str(frontend))
    (dist_dir / web_handler.FRONTEND_BUILD_FINGERPRINT_FILENAME).write_text(
        json.dumps({"fingerprint": source_fingerprint}) + "\n",
        encoding="utf-8",
    )

    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    ok, output = _build(workspace)

    assert ok is True
    assert recorder.calls == ["npm run build"]
    assert "Reused the existing" not in output


def test_fingerprint_ignores_build_output_and_dependencies(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    frontend = workspace / "frontend"
    before = web_handler._frontend_source_fingerprint(str(frontend))

    (frontend / "node_modules" / "pkg").mkdir(parents=True)
    (frontend / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    (frontend / "dist").mkdir()
    (frontend / "dist" / "index.html").write_text("x\n", encoding="utf-8")

    assert web_handler._frontend_source_fingerprint(str(frontend)) == before


def test_missing_frontend_directory_has_no_fingerprint(tmp_path) -> None:
    assert web_handler._frontend_source_fingerprint(str(tmp_path / "absent")) is None


def test_linked_source_directory_is_included_in_fingerprint(tmp_path) -> None:
    """A symlinked frontend/src must contribute to the build fingerprint.

    Default os.walk skips directory symlinks, so a linked source tree could
    leave E2E tests reusing a stale dist. The fingerprint must hash the linked
    contents and change when they change.
    """

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    shared_src = tmp_path / "shared-src"
    (shared_src).mkdir()
    (shared_src / "main.js").write_text("console.log('v1')\n", encoding="utf-8")
    _try_symlink_to(frontend / "src", shared_src)
    (frontend / "package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")

    before = web_handler._frontend_source_fingerprint(str(frontend))
    assert before is not None

    # Editing the file behind the symlink must change the fingerprint.
    (shared_src / "main.js").write_text("console.log('v2')\n", encoding="utf-8")
    after = web_handler._frontend_source_fingerprint(str(frontend))

    assert after is not None
    assert after != before


def test_build_rebuilds_when_linked_source_changes(tmp_path, monkeypatch) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    shared_src = tmp_path / "shared-src"
    (shared_src).mkdir()
    (shared_src / "main.js").write_text("console.log('v1')\n", encoding="utf-8")
    _try_symlink_to(frontend / "src", shared_src)
    (frontend / "package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")

    recorder = _BuildRecorder()
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    first_ok, _ = _build(tmp_path)
    assert first_ok
    assert recorder.calls == ["npm run build"]

    (shared_src / "main.js").write_text("console.log('v2')\n", encoding="utf-8")
    second_ok, second_output = _build(tmp_path)

    assert second_ok
    assert recorder.calls == ["npm run build", "npm run build"]
    assert "Reused the existing" not in second_output

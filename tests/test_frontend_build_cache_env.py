"""Frontend build cache keys must include environment-driven build inputs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app_type_handler import web as web_handler


class _BuildRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, command: str, cwd: str, timeout: float = 60.0, extra_env=None) -> web_handler._CommandResult:
        self.calls.append(command)
        dist_dir = Path(cwd) / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
        return web_handler._CommandResult(exit_code=0, text="Exit Code: 0\n")


def test_rebuilds_when_frontend_build_environment_changes(tmp_path: Path, monkeypatch) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "main.js").write_text("console.log('v1')\n", encoding="utf-8")
    (frontend / "package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")
    recorder = _BuildRecorder()
    build_env = {"VITE_API_BASE_URL": "http://localhost:3301"}
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)
    monkeypatch.setattr(web_handler, "build_web_runtime_env", lambda: dict(build_env))

    asyncio.run(web_handler._build_frontend_dist(str(tmp_path)))
    build_env["VITE_API_BASE_URL"] = "http://localhost:4401"
    asyncio.run(web_handler._build_frontend_dist(str(tmp_path)))

    assert recorder.calls == ["npm run build", "npm run build"]

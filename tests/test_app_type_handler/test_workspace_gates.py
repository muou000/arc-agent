"""Workspace gates must stop a broken scaffold before the node loop starts.

A blank templates root used to be invisible: `copy_template` only checked that
the directory existed, npm exited 0 without installing anything, and the empty
`node_modules` was reported as "packages ready". Every requirement then failed
its build and test steps, burning the full TDD retry budget 143 times over.

These tests pin the three guards that make that failure mode impossible:

1. `node_modules_ready` - a zero exit code is not proof that packages landed;
2. `run_npm_install` - falls back to `--legacy-peer-deps` and reports failures;
3. `copy_template` / `verify_workspace` - refuse an unusable template and a
   workspace whose frontend cannot build.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app_type_handler import base as base_handler
from app_type_handler import create_app_type_handler, web as web_handler


def _noop_log(*_args, **_kwargs) -> None:
    return None


def _make_handler(workspace: Path):
    return create_app_type_handler(
        app_type="web",
        workspace_path=str(workspace),
        requirement_path=str(workspace / "requirements.yaml"),
        interface_designer=None,
        log_cb=_noop_log,
    )


def _install_packages(target_dir: Path, name: str = "react") -> None:
    package = target_dir / "node_modules" / name
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name": "react"}\n', encoding="utf-8")


def _write_template(root: Path, *, with_manifest: bool = True, with_source: bool = True) -> Path:
    template = root / "web-react-express"
    (template / "backend" / "src").mkdir(parents=True)
    if with_manifest:
        (template / "template.yaml").write_text("id: web-react-express\n", encoding="utf-8")
    if with_source:
        (template / "backend" / "src" / "index.js").write_text("// app\n", encoding="utf-8")
    return template


# --------------------------------------------------------------------------
# node_modules_ready
# --------------------------------------------------------------------------


def test_node_modules_ready_rejects_a_missing_directory(tmp_path) -> None:
    assert web_handler.node_modules_ready(str(tmp_path)) is False


def test_node_modules_ready_rejects_an_empty_directory(tmp_path) -> None:
    (tmp_path / "node_modules").mkdir()
    assert web_handler.node_modules_ready(str(tmp_path)) is False


def test_node_modules_ready_ignores_dotfiles(tmp_path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text("{}\n", encoding="utf-8")
    assert web_handler.node_modules_ready(str(tmp_path)) is False


def test_node_modules_ready_accepts_an_installed_tree(tmp_path) -> None:
    _install_packages(tmp_path)
    assert web_handler.node_modules_ready(str(tmp_path)) is True


# --------------------------------------------------------------------------
# run_npm_install
# --------------------------------------------------------------------------


def test_install_succeeds_on_the_first_attempt(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        _install_packages(Path(target_dir))
        return 0, "added 10 packages", ""

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is True
    assert calls == ["npm install"]


def test_install_falls_back_to_legacy_peer_deps(tmp_path, monkeypatch) -> None:
    """npm 10.x wedges or crashes arborist while resolving vitest's optional peers."""

    calls: list[tuple[str, float]] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append((command, timeout))
        if web_handler.LEGACY_PEER_DEPS_FLAG in command:
            _install_packages(Path(target_dir))
            return 0, "added 215 packages", ""
        return 1, "", "Cannot read properties of null (reading 'edgesOut')"

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is True
    assert [command for command, _ in calls] == [
        "npm install",
        f"npm install {web_handler.LEGACY_PEER_DEPS_FLAG}",
    ]
    # The primary attempt must not be allowed to stall for the full budget.
    assert calls[0][1] < calls[1][1]


def test_install_fails_when_npm_exits_zero_without_installing(tmp_path, monkeypatch) -> None:
    """This is the exact bug that made a blank workspace look healthy."""

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        return 0, "up to date in 3s", ""

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is False


def test_install_failure_message_keeps_the_npm_error(tmp_path, monkeypatch) -> None:
    messages: list[tuple] = []

    def recording_log(agent_name, message, status=None, node_id=None):
        messages.append((agent_name, message, status))

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        return 1, "", "npm error ERESOLVE could not resolve"

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), recording_log)) is False

    failure = [m for m in messages if "NPM install failed" in m[1]]
    assert len(failure) == 1
    assert failure[0][2] == "error"
    assert "ERESOLVE" in failure[0][1]


# --------------------------------------------------------------------------
# template usability
# --------------------------------------------------------------------------


def test_template_without_a_manifest_is_not_usable(tmp_path) -> None:
    template = _write_template(tmp_path, with_manifest=False)
    assert web_handler.WebAppType._template_looks_usable(str(template)) is False


def test_empty_template_tree_is_not_usable(tmp_path) -> None:
    template = tmp_path / "web-react-express"
    (template / "backend" / "src").mkdir(parents=True)
    assert web_handler.WebAppType._template_looks_usable(str(template)) is False


def test_manifest_only_template_is_not_usable(tmp_path) -> None:
    """The manifest alone still yields a blank workspace."""

    template = _write_template(tmp_path, with_source=False)
    assert web_handler.WebAppType._template_looks_usable(str(template)) is False


def test_complete_template_is_usable(tmp_path) -> None:
    template = _write_template(tmp_path)
    assert web_handler.WebAppType._template_looks_usable(str(template)) is True


def test_copy_template_refuses_an_empty_template_tree(tmp_path, monkeypatch) -> None:
    templates_root = tmp_path / "templates"
    _write_template(templates_root, with_source=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("ARC_AGENT_TEMPLATES_ROOT", str(templates_root))
    handler = _make_handler(workspace)

    assert asyncio.run(handler.copy_template()) is False
    assert list(workspace.iterdir()) == []


def test_copy_template_accepts_a_complete_template(tmp_path, monkeypatch) -> None:
    templates_root = tmp_path / "templates"
    _write_template(templates_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("ARC_AGENT_TEMPLATES_ROOT", str(templates_root))
    handler = _make_handler(workspace)

    assert asyncio.run(handler.copy_template()) is True
    assert (workspace / "template.yaml").is_file()
    assert (workspace / "backend" / "src" / "index.js").is_file()


# --------------------------------------------------------------------------
# verify_workspace smoke check
# --------------------------------------------------------------------------


def test_verify_workspace_passes_when_the_frontend_builds(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        return "Exit Code: 0\nSTDOUT:\nbuilt\n"

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True


def test_verify_workspace_fails_when_the_frontend_build_fails(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        return "Exit Code: 1\nSTDERR:\n'vite' is not recognized\n"

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Exit Code: 0\nSTDOUT:\nok\n", 0),
        ("Exit Code: 1\nSTDERR:\nboom\n", 1),
        ("no exit code here", None),
    ],
)
def test_extract_exit_code(raw: str, expected) -> None:
    assert web_handler._extract_exit_code(raw) == expected


# --------------------------------------------------------------------------
# template location
# --------------------------------------------------------------------------


def test_template_candidates_point_at_the_official_layout(monkeypatch) -> None:
    monkeypatch.delenv("ARC_AGENT_TEMPLATES_ROOT", raising=False)
    assert base_handler.template_candidates("web-react-express") == [
        str(Path(base_handler.REPO_ROOT) / "arc-template" / "templates" / "web-react-express")
    ]


@pytest.mark.parametrize("template_id", ["mobile-android-java", "cli-python"])
def test_each_app_type_gets_its_own_directory(template_id: str, monkeypatch) -> None:
    """One directory per app type, with no shared fallback to cross wires."""

    monkeypatch.delenv("ARC_AGENT_TEMPLATES_ROOT", raising=False)
    assert base_handler.template_candidates(template_id) == [
        str(Path(base_handler.REPO_ROOT) / "arc-template" / "templates" / template_id)
    ]


def test_template_candidates_honour_an_explicit_root(tmp_path, monkeypatch) -> None:
    """An explicit root is authoritative - never silently fall back past it."""

    monkeypatch.setenv("ARC_AGENT_TEMPLATES_ROOT", str(tmp_path))
    assert base_handler.template_candidates("web-react-express") == [
        str(tmp_path / "web-react-express")
    ]


def test_template_dir_reports_the_path_when_nothing_is_usable(tmp_path, monkeypatch) -> None:
    empty = tmp_path / "arc-template" / "templates" / "web-react-express"
    (empty / "backend" / "src").mkdir(parents=True)

    monkeypatch.setattr(
        base_handler,
        "template_candidates",
        lambda template_id: [str(empty)],
    )

    assert web_handler.WebAppType.template_dir() == str(empty)


def test_shipped_template_is_present_and_usable(monkeypatch) -> None:
    """Guards the original outage: an empty templates root scaffolded a blank
    workspace, and every requirement then failed its build and its tests.
    """

    monkeypatch.delenv("ARC_AGENT_TEMPLATES_ROOT", raising=False)
    template = web_handler.WebAppType.template_dir()

    assert Path(template).is_dir(), f"template directory missing: {template}"
    assert web_handler.WebAppType._template_looks_usable(template) is True


def test_template_dir_reports_the_primary_path_when_nothing_is_usable(tmp_path, monkeypatch) -> None:
    primary = tmp_path / "arc-template" / "templates" / "web-react-express"
    empty = tmp_path / "legacy-root" / "web-react-express"
    (empty / "backend" / "src").mkdir(parents=True)

    monkeypatch.setattr(
        base_handler,
        "template_candidates",
        lambda template_id: [str(primary), str(empty)],
    )

    assert web_handler.WebAppType.template_dir() == str(primary)


# --------------------------------------------------------------------------
# shipped template contract
# --------------------------------------------------------------------------


def test_shipped_template_declares_testing_library_dom() -> None:
    """`@testing-library/react` 16 lists `@testing-library/dom` as a peer.

    The install path falls back to `--legacy-peer-deps` (npm 10 arborist crashes
    while resolving vitest's optional peers), and that flag skips peer
    installation. Unless the template declares the peer directly, every
    component test fails to import `@testing-library/dom`, the agent has no way
    to install it, and the node burns its whole retry budget on an
    un-fixable-by-design failure.
    """

    template = Path(web_handler.WebAppType.template_dir())
    package = json.loads((template / "frontend" / "package.json").read_text(encoding="utf-8"))
    declared = set(package.get("dependencies") or {}) | set(package.get("devDependencies") or {})

    assert "@testing-library/dom" in declared

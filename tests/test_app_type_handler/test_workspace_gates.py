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
import os
import shutil
import sys
from pathlib import Path

import pytest

from app_type_handler import base as base_handler
from app_type_handler import create_app_type_handler, template_patches, web as web_handler
from app_type_handler import backend_runtime as backend_runtime_module
from app_type_handler.backend_runtime import _CommandResult
from app_type_handler.template_patches import (
    ALREADY_APPLIED,
    APPLIED,
    SKIPPED,
    UNRECOGNIZED,
    apply_template_patches,
)


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


@pytest.mark.parametrize(
    "returncode,stdout,stderr",
    [
        (1, "", "npm error ERESOLVE unable to resolve dependency tree"),
        (1, "", "TypeError: Cannot read properties of null (reading 'edgesOut')"),
    ],
)
def test_install_retries_known_peer_resolution_failures(
    tmp_path, monkeypatch, returncode, stdout, stderr
) -> None:
    calls: list[str] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        if web_handler.LEGACY_PEER_DEPS_FLAG in command:
            _install_packages(Path(target_dir))
            return 0, "installed", ""
        return returncode, stdout, stderr

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is True
    assert calls == [
        "npm install",
        f"npm install {web_handler.LEGACY_PEER_DEPS_FLAG}",
    ]


@pytest.mark.parametrize(
    "returncode,stdout,stderr",
    [
        (1, "", "npm error EACCES permission denied"),
        (1, "", "npm error ECONNRESET registry connection lost"),
        (1, "", "npm error EJSONPARSE malformed package.json"),
        (127, "", "npm: command not found"),
        (1, "", "npm error unknown install failure"),
    ],
)
def test_install_does_not_retry_unrelated_failures(
    tmp_path, monkeypatch, returncode, stdout, stderr
) -> None:
    calls: list[str] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        return returncode, stdout, stderr

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is False
    assert calls == ["npm install"]


def test_install_does_not_retry_when_npm_cannot_be_spawned(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        raise FileNotFoundError("npm executable missing")

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is False
    assert calls == ["npm install"]


def test_final_install_failure_preserves_both_attempt_diagnostics(tmp_path, monkeypatch) -> None:
    messages: list[tuple] = []
    calls: list[str] = []

    def recording_log(agent_name, message, status=None, node_id=None):
        messages.append((agent_name, message, status))

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        if len(calls) == 1:
            return 1, "primary stdout details", "ERESOLVE unable to resolve dependency tree"
        return 23, "fallback stdout details", "fallback diagnostic"

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), recording_log)) is False
    assert calls == [
        "npm install",
        f"npm install {web_handler.LEGACY_PEER_DEPS_FLAG}",
    ]
    failure = next(m[1] for m in messages if "NPM install failed" in m[1])
    assert "npm install exited 1" in failure
    assert "primary stdout details" in failure
    assert "ERESOLVE unable to resolve dependency tree" in failure
    assert f"npm install {web_handler.LEGACY_PEER_DEPS_FLAG} exited 23" in failure
    assert "fallback stdout details" in failure
    assert "fallback diagnostic" in failure


def test_install_uses_at_most_one_legacy_peer_retry(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        calls.append(command)
        return 1, "", "ERESOLVE unable to resolve dependency tree"

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), _noop_log)) is False
    assert calls == [
        "npm install",
        f"npm install {web_handler.LEGACY_PEER_DEPS_FLAG}",
    ]


def test_install_fails_when_npm_exits_zero_without_installing(tmp_path, monkeypatch) -> None:
    """This is the exact bug that made a blank workspace look healthy."""
    messages: list[tuple] = []

    def recording_log(agent_name, message, status=None, node_id=None):
        messages.append((agent_name, message, status))

    async def fake_run(command: str, target_dir: str, timeout: float = 0.0):
        return 0, "up to date in 3s", "npm notice diagnostic"

    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run)

    assert asyncio.run(web_handler.run_npm_install(str(tmp_path), recording_log)) is False
    failure = next(m[1] for m in messages if "NPM install failed" in m[1])
    assert "npm install exited 0" in failure
    assert "up to date in 3s" in failure
    assert "npm notice diagnostic" in failure


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
# install_dependencies patches the missing testing-library peer
# --------------------------------------------------------------------------


def test_install_dependencies_installs_the_missing_dom_peer(tmp_path, monkeypatch) -> None:
    """`@testing-library/react` 16 needs the `@testing-library/dom` peer, the
    provided template does not declare it, and the `--legacy-peer-deps`
    fallback skips peer installation - so the runtime patches it in with
    `--no-save --no-package-lock`, leaving the provided template files
    untouched."""

    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "backend").mkdir()
    handler = _make_handler(workspace)

    async def fake_run_npm_install(target_dir, log_cb):
        return True

    npm_commands: list[str] = []

    async def fake_run_npm_command(command, target_dir, timeout=0.0):
        npm_commands.append(command)
        return 0, "", ""

    monkeypatch.setattr(web_handler, "run_npm_install", fake_run_npm_install)
    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run_npm_command)

    assert asyncio.run(handler.install_dependencies()) is True
    assert len(npm_commands) == 1
    assert "--no-save --no-package-lock" in npm_commands[0]
    assert "@testing-library/dom" in npm_commands[0]


def test_install_dependencies_skips_the_dom_peer_when_installed(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    frontend = workspace / "frontend"
    (frontend / "node_modules" / "@testing-library" / "dom").mkdir(parents=True)
    (workspace / "backend").mkdir()
    handler = _make_handler(workspace)

    async def fake_run_npm_install(target_dir, log_cb):
        return True

    npm_commands: list[str] = []

    async def fake_run_npm_command(command, target_dir, timeout=0.0):
        npm_commands.append(command)
        return 0, "", ""

    monkeypatch.setattr(web_handler, "run_npm_install", fake_run_npm_install)
    monkeypatch.setattr(web_handler, "_run_npm_command", fake_run_npm_command)

    assert asyncio.run(handler.install_dependencies()) is True
    assert npm_commands == []


# --------------------------------------------------------------------------
# verify_workspace smoke check
# --------------------------------------------------------------------------


def test_verify_workspace_passes_when_the_frontend_builds(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nbuilt\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True


def test_verify_workspace_fails_when_the_frontend_build_fails(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        return _CommandResult(exit_code=1, text="Exit Code: 1\nSTDERR:\n'vite' is not recognized\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is False


# --------------------------------------------------------------------------
# verify_workspace provisions the E2E runner
# --------------------------------------------------------------------------


def _make_runnable_workspace(tmp_path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "backend").mkdir(parents=True)
    return workspace


def test_verify_workspace_installs_playwright_browsers(tmp_path, monkeypatch) -> None:
    """`npm install` never downloads the browser binaries.

    Without this step the first E2E run of the first leaf node fails with
    "Executable doesn't exist", and the agent cannot recover on its own:
    `execute` is disabled, so it has no way to run `playwright install`.
    """

    workspace = _make_runnable_workspace(tmp_path)
    handler = _make_handler(workspace)
    commands: list[str] = []

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        commands.append(command)
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nok\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True
    assert commands[0] == "npm run build"
    assert "e2e:install-browsers" in commands[1]


def test_verify_workspace_aborts_when_browsers_cannot_be_installed(tmp_path, monkeypatch) -> None:
    workspace = _make_runnable_workspace(tmp_path)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        if "e2e:install-browsers" in command:
            return _CommandResult(
                exit_code=1,
                text='Exit Code: 1\nSTDERR:\nnpm error Missing script: "e2e:install-browsers"\n',
            )
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nbuilt\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is False


def test_verify_workspace_skips_the_browser_gate_without_a_backend(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "frontend").mkdir(parents=True)
    handler = _make_handler(workspace)

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        assert command == "npm run build"
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nbuilt\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True


def test_verify_workspace_skips_the_browser_gate_when_opted_out(tmp_path, monkeypatch) -> None:
    """`ARC_SKIP_BROWSER_INSTALL` is the escape hatch for hosts that cannot
    (or should not) download the browser binaries; compilation proceeds and
    only E2E runs will fail on a missing browser."""
    workspace = _make_runnable_workspace(tmp_path)
    handler = _make_handler(workspace)
    commands: list[str] = []

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        commands.append(command)
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nbuilt\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)
    monkeypatch.setenv("ARC_SKIP_BROWSER_INSTALL", "1")

    assert asyncio.run(handler.verify_workspace()) is True
    assert commands == ["npm run build"]


def test_verify_workspace_falls_back_to_npx_without_an_install_script(tmp_path, monkeypatch) -> None:
    """The officially provisioned template does not declare an
    `e2e:install-browsers` script; the equivalent `npx` invocation keeps browser
    provisioning working without requiring any template file to change."""

    workspace = _make_runnable_workspace(tmp_path)
    (workspace / "backend" / "package.json").write_text(
        json.dumps({"name": "backend", "scripts": {"start": "node src/index.js"}}),
        encoding="utf-8",
    )
    handler = _make_handler(workspace)
    commands: list[str] = []

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        commands.append(command)
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nok\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True
    assert commands[0] == "npm run build"
    assert commands[1] == "npx playwright install chromium chromium-headless-shell"


def test_verify_workspace_uses_the_install_script_when_declared(tmp_path, monkeypatch) -> None:
    workspace = _make_runnable_workspace(tmp_path)
    (workspace / "backend" / "package.json").write_text(
        json.dumps(
            {
                "name": "backend",
                "scripts": {"e2e:install-browsers": "playwright install chromium"},
            }
        ),
        encoding="utf-8",
    )
    handler = _make_handler(workspace)
    commands: list[str] = []

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        commands.append(command)
        return _CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nok\n")

    monkeypatch.setattr(web_handler, "_execute_web_test_command", fake_command)

    assert asyncio.run(handler.verify_workspace()) is True
    assert commands[1] == "npm run e2e:install-browsers"


# --------------------------------------------------------------------------
# post_template_setup verifies the port contract
# --------------------------------------------------------------------------


def _write_runtime_port_files(workspace: Path, *, playwright_port: str) -> None:
    (workspace / "backend" / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "frontend").mkdir(parents=True, exist_ok=True)
    (workspace / "backend" / "src" / "index.js").write_text(
        "const port = Number(process.env.PORT || 3000);\n", encoding="utf-8"
    )
    (workspace / "frontend" / "vite.config.js").write_text(
        "const backendPort = Number(process.env.ARC_WEB_PORT || 3000)\n", encoding="utf-8"
    )
    (workspace / "backend" / "playwright.config.js").write_text(
        playwright_port, encoding="utf-8"
    )


def test_post_template_setup_accepts_environment_driven_ports(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    _write_runtime_port_files(
        workspace,
        playwright_port=(
            "const baseURL = process.env.PLAYWRIGHT_BASE_URL\n"
            "  || `http://127.0.0.1:${process.env.ARC_WEB_PORT || 3000}`;\n"
        ),
    )
    handler = _make_handler(workspace)

    assert asyncio.run(handler.post_template_setup()) is True


def test_post_template_setup_accepts_the_official_playwright_origin_env(tmp_path) -> None:
    """The officially provisioned template (ARC-Bench runner image) resolves the
    Playwright origin from `PLAYWRIGHT_BASE_URL`/`ARC_WEB_BASE_URL`, which the
    E2E runner always exports. Only a config with no env-driven origin at all
    fails the gate.
    """

    workspace = tmp_path / "workspace"
    _write_runtime_port_files(
        workspace,
        playwright_port=(
            "const baseURL = process.env.PLAYWRIGHT_BASE_URL\n"
            "  || process.env.ARC_WEB_BASE_URL\n"
            "  || 'http://127.0.0.1:3000';\n"
        ),
    )
    handler = _make_handler(workspace)

    assert asyncio.run(handler.post_template_setup()) is True


def test_post_template_setup_rejects_a_hardcoded_playwright_port(tmp_path) -> None:
    """The exact drift that made every E2E run navigate to a dead origin.

    The old implementation substituted a `__ARC_WEB_PORT__` token that no
    template file contained, so it reported success while `playwright.config.js`
    kept its own hardcoded port.
    """

    workspace = tmp_path / "workspace"
    _write_runtime_port_files(
        workspace,
        playwright_port="const baseURL = 'http://127.0.0.1:3000';\n",
    )
    handler = _make_handler(workspace)
    messages: list[tuple] = []

    def recording_log(agent_name, message, status=None, node_id=None):
        messages.append((agent_name, message, status))

    handler.log_cb = recording_log

    assert asyncio.run(handler.post_template_setup()) is False
    failures = [m for m in messages if m[2] == "error"]
    assert len(failures) == 1
    assert "playwright.config.js" in failures[0][1]


def test_post_template_setup_tolerates_absent_runtime_files(tmp_path) -> None:
    """A partial template must not fail the gate on files it never shipped."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handler = _make_handler(workspace)

    assert asyncio.run(handler.post_template_setup()) is True


# --------------------------------------------------------------------------
# E2E runtime environment
# --------------------------------------------------------------------------


def test_e2e_runtime_env_points_playwright_at_the_workspace_port(tmp_path) -> None:
    """`PLAYWRIGHT_BASE_URL` is documented to the agent but was never set."""

    env = web_handler._build_e2e_runtime_env(str(tmp_path), ["backend/test-e2e/home.spec.js"])

    assert env["PLAYWRIGHT_BASE_URL"] == f"http://127.0.0.1:{web_handler.get_web_port()}"
    assert env["ARC_WEB_PORT"] == str(web_handler.get_web_port())
    assert env["ARC_E2E_DB_PATH"].endswith(".sqlite")


def test_e2e_db_filename_truncates_the_suite_label_and_keeps_the_hash(tmp_path) -> None:
    """The unbounded target-list label must not walk the file to MAX_PATH.

    A serial run's four E2E specs spelled a ~140-char basename; on Windows the
    directory prefix and sqlite's `-wal`/`-shm` siblings pushed the full path
    toward the 260-char limit. The label is a short readability prefix only —
    the suite hash carries the isolation.
    """

    long_targets = [
        f"backend/test-e2e/booking-flow-scenario-{index}-with-a-very-long-descriptive-name.spec.ts"
        for index in range(4)
    ]

    env = web_handler._build_e2e_runtime_env(str(tmp_path), long_targets)

    basename = os.path.basename(env["ARC_E2E_DB_PATH"])
    assert basename.endswith(".sqlite")
    label, _separator, suite_hash = basename[: -len(".sqlite")].rpartition("-")
    assert len(label) <= backend_runtime_module._E2E_DB_SUITE_LABEL_MAX_LENGTH
    assert len(suite_hash) == 10


def test_e2e_db_filename_hash_keeps_suite_isolation_after_truncation(tmp_path) -> None:
    """Target sets truncating to the same label still land on different files."""

    prefix = "backend/test-e2e/booking-flow-scenario-with-a-very-long-descriptive-name"
    first = web_handler._build_e2e_runtime_env(str(tmp_path), [f"{prefix}-a.spec.ts"])
    second = web_handler._build_e2e_runtime_env(str(tmp_path), [f"{prefix}-b.spec.ts"])
    again = web_handler._build_e2e_runtime_env(str(tmp_path), [f"{prefix}-a.spec.ts"])

    assert first["ARC_E2E_DB_PATH"] != second["ARC_E2E_DB_PATH"]
    assert first["ARC_E2E_DB_PATH"] == again["ARC_E2E_DB_PATH"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, 0), (3, 3)],
)
def test_execute_web_test_command_surfaces_structural_exit_codes(code: int, expected: int, tmp_path) -> None:
    """The command runner carries the exit code structurally, not only in text."""

    result = asyncio.run(
        web_handler._execute_web_test_command(
            f'"{sys.executable}" -c "raise SystemExit({code})"',
            cwd=str(tmp_path),
        )
    )

    assert result.exit_code == expected
    assert f"Exit Code: {expected}" in result.text


def test_execute_web_test_command_reports_spawn_failure_without_a_code(tmp_path, monkeypatch) -> None:
    """A command that never spawns (OSError) carries no exit code at all."""

    async def refuse_to_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(web_handler.asyncio, "create_subprocess_shell", refuse_to_spawn)

    result = asyncio.run(
        web_handler._execute_web_test_command(
            "definitely-not-a-real-command-xyz",
            cwd=str(tmp_path),
        )
    )

    assert result.exit_code is None
    assert "Execution failed" in result.text


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


def test_shipped_template_declares_testing_library_react() -> None:
    """`@testing-library/react` 16 lists `@testing-library/dom` as a peer.

    The officially provisioned template does not declare the peer, and the
    `--legacy-peer-deps` install fallback skips peer installation - so
    `install_dependencies` patches `@testing-library/dom` in with
    `--no-save --no-package-lock` (pinned by
    `test_install_dependencies_installs_the_missing_dom_peer`). This test pins
    the other half of the contract: the template keeps declaring the package
    that needs the peer, so the runtime patch stays load-bearing.
    """

    template = Path(web_handler.WebAppType.template_dir())
    package = json.loads((template / "frontend" / "package.json").read_text(encoding="utf-8"))
    declared = set(package.get("dependencies") or {}) | set(package.get("devDependencies") or {})

    assert "@testing-library/react" in declared


def test_shipped_template_declares_playwright_tooling() -> None:
    """The E2E runner and the browser-install CLI must both be installed.

    The officially provisioned template does not declare an
    `e2e:install-browsers` script, so `_verify_e2e_runner` falls back to
    `npx playwright install chromium chromium-headless-shell`. That only works
    when the template ships both halves of the toolchain: `playwright` (the
    CLI that downloads the browsers) and `@playwright/test` (the runner that
    launches them).
    """

    template = Path(web_handler.WebAppType.template_dir())
    package = json.loads((template / "backend" / "package.json").read_text(encoding="utf-8"))
    dependencies = package.get("devDependencies") or {}

    assert "playwright" in dependencies
    assert "@playwright/test" in dependencies


def test_shipped_template_playwright_ranges_float_together() -> None:
    """The browser build the CLI downloads must match the runner's expectation.

    `playwright` and `@playwright/test` pin the browser build revision. The
    officially provisioned template declares different carets (^1.28.0 vs
    ^1.57.0), which is safe only because both ranges float to the same latest
    1.x release at install time - npm resolves them to identical versions, so
    the downloaded build is the one the runner looks for. Pin both to the same
    floating-1.x shape; exact pins would reintroduce the mismatch risk.
    """

    template = Path(web_handler.WebAppType.template_dir())
    package = json.loads((template / "backend" / "package.json").read_text(encoding="utf-8"))
    dependencies = package.get("devDependencies") or {}

    assert dependencies.get("playwright", "").startswith("^1.")
    assert dependencies.get("@playwright/test", "").startswith("^1.")


def test_shipped_template_playwright_config_reads_the_origin_from_the_environment() -> None:
    """The config must resolve the origin under test from the environment.

    `web.py` exports `PLAYWRIGHT_BASE_URL` for every E2E run; a config with no
    env-driven origin would send Playwright to a hardcoded port the workspace
    never serves on.
    """

    template = Path(web_handler.WebAppType.template_dir())
    config = (template / "backend" / "playwright.config.js").read_text(encoding="utf-8")

    assert "process.env.PLAYWRIGHT_BASE_URL" in config


# --------------------------------------------------------------------------
# shipped template fixes are delivered at scaffold time
# --------------------------------------------------------------------------
#
# The template is provisioned by the platform (`ARC_AGENT_TEMPLATES_ROOT`), so a
# fix that must reach every generated workspace cannot be a repo template edit:
# it lives in `app_type_handler.template_patches` and is applied to the copy.


SHIPPED_TEMPLATE_ROOT = (
    Path(base_handler.REPO_ROOT) / "arc-template" / "templates" / "web-react-express"
)


def _patched_file_contents(workspace: Path) -> dict[str, str]:
    return {
        name: (workspace / name).read_text(encoding="utf-8")
        for name in (
            "backend/src/database/init_db.js",
            "backend/src/database/test_harness.js",
            "README.md",
            "backend/src/app.js",
        )
    }


def test_post_template_setup_applies_the_shipped_template_fixes(tmp_path, monkeypatch) -> None:
    """A workspace scaffolded from the template carries the fixed bootstrap.

    The fix exists only in `template_patches`, so without this step every
    generated app keeps the defect and every E2E attempt in every node pays for
    it.
    """

    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, templates_root / "web-react-express")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ARC_AGENT_TEMPLATES_ROOT", str(templates_root))
    handler = _make_handler(workspace)

    assert asyncio.run(handler.copy_template()) is True
    assert asyncio.run(handler.post_template_setup()) is True

    bootstrap = (workspace / "backend" / "src" / "database" / "init_db.js").read_text(
        encoding="utf-8"
    )
    assert "const MAX_INIT_ATTEMPTS = 5;" in bootstrap
    assert "function startInit() {" in bootstrap
    assert "return initPromise;" not in bootstrap

    harness = (workspace / "backend" / "src" / "database" / "test_harness.js").read_text(
        encoding="utf-8"
    )
    assert "const scopeDir = path.join(rootDir," in harness

    gitignore_lines = (workspace / "backend" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()
    assert "test-results" in gitignore_lines
    assert "playwright-report" in gitignore_lines

    app_module = (workspace / "backend" / "src" / "app.js").read_text(encoding="utf-8")
    assert "res.sendFile(path.join(frontendDistPath, 'index.html'), { dotfiles: 'allow' });" in app_module


def test_shipped_template_fixes_are_idempotent(tmp_path) -> None:
    """A resumed compile must neither re-apply nor rewrite anything."""

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)

    first = apply_template_patches(str(workspace), "web-react-express")
    assert [outcome.status for outcome in first] == [
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]
    after_first = _patched_file_contents(workspace)

    second = apply_template_patches(str(workspace), "web-react-express")
    assert [outcome.status for outcome in second] == [
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
    ]
    assert _patched_file_contents(workspace) == after_first


def test_shipped_template_fixes_refuse_an_unknown_shape_atomically(tmp_path) -> None:
    """A template that evolved upstream is reported, never overwritten.

    The unrecognized file must stay byte-for-byte as it was, and a sibling file
    edited by the same patch must not be touched either: a half-applied patch
    would leave a workspace that is neither the old nor the new state. An
    independent patch with intact targets (the harness guard) still applies -
    one unrecognizable file stops its own patch, not the registration.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    bootstrap = workspace / "backend" / "src" / "database" / "init_db.js"
    bootstrap.write_text("// rewritten upstream\n", encoding="utf-8")
    readme = workspace / "README.md"
    readme_before = readme.read_text(encoding="utf-8")

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        UNRECOGNIZED,
        UNRECOGNIZED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]
    assert bootstrap.read_text(encoding="utf-8") == "// rewritten upstream\n"
    assert readme.read_text(encoding="utf-8") == readme_before


def test_harness_fix_refuses_an_unknown_shape_and_keeps_the_file(tmp_path) -> None:
    """The harness guard reports an evolved test_harness.js instead of clobbering it.

    The same refusal contract as the bootstrap patches, pinned for their own
    target file: the unrecognized file stays byte-for-byte as it was while the
    independent bootstrap patches still apply.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    harness = workspace / "backend" / "src" / "database" / "test_harness.js"
    harness.write_text("// rewritten upstream\n", encoding="utf-8")

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        APPLIED,
        APPLIED,
        UNRECOGNIZED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]
    assert "test_harness.js" in outcomes[2].detail
    assert harness.read_text(encoding="utf-8") == "// rewritten upstream\n"


def test_templates_without_registered_fixes_are_untouched(tmp_path) -> None:
    assert apply_template_patches(str(tmp_path), "cli-python") == []


def test_repo_template_readme_documents_the_runtime_contract(tmp_path) -> None:
    """The contract line must reach the generated workspace's README.

    The repo mirror now tracks the official template, so the pre-fix README
    carries no contract line of its own; the patches deliver it, and a
    maintainer reading a scaffolded workspace has to see what behavior ARC
    guarantees and where the fix lives.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    outcomes = apply_template_patches(str(workspace), "web-react-express")
    assert all(outcome.status == APPLIED for outcome in outcomes), outcomes
    readme = (workspace / "README.md").read_text(encoding="utf-8")

    assert "never returns a closed handle" in readme
    assert "template_patches.py" in readme


def test_repo_template_tracks_the_official_prefix_shapes() -> None:
    """The mirror must ship the shapes the patch chain searches for.

    The patch search shapes assume the official provisioned template's pre-fix
    content. On 2026-09-18 the mirror had drifted ahead of it (it carried a
    repo-only fix the platform never received), the first edit matched nothing,
    and every online run aborted at scaffold time. Pinning the pre-fix shape
    here turns that drift into a local test failure instead.
    """

    mirror = (SHIPPED_TEMPLATE_ROOT / "backend" / "src" / "database" / "init_db.js").read_text(
        encoding="utf-8"
    ).replace("\r\n", "\n")

    assert "  if (initPromise) {\n    return initPromise;\n  }\n" in mirror
    assert "function startInit() {" not in mirror
    assert "const MAX_INIT_ATTEMPTS" not in mirror


def test_repo_template_harness_tracks_the_official_prefix_shape() -> None:
    """The harness guard patch searches for shapes the official template ships.

    Same drift contract as ``test_repo_template_tracks_the_official_prefix_shapes``
    pinned for its own target file: the mirror must carry the pre-fix harness -
    a single mkdir in ``setup()``, no scope directory - or the guard's search
    shapes no longer match the provisioned template and every online run would
    report the patch as unrecognized at scaffold time.
    """

    mirror = (SHIPPED_TEMPLATE_ROOT / "backend" / "src" / "database" / "test_harness.js").read_text(
        encoding="utf-8"
    ).replace("\r\n", "\n")

    assert "scopeDir" not in mirror
    assert "Re-create the root directory before reopening sqlite" not in mirror
    assert mirror.count("fs.mkdirSync(rootDir, { recursive: true });") == 1
    assert "    removeRootDirWhenEmpty: true,\n" in mirror


def test_repo_template_app_tracks_the_official_prefix_shape() -> None:
    """The SPA fallback patch searches for shapes the official template ships.

    Same drift contract as the bootstrap and harness prefix pins: the mirror's
    ``backend/src/app.js`` must carry the bare root-less ``res.sendFile`` the
    patch replaces - a mirror that already carries the fix (or a differently
    shaped fallback) would make every online run report the patch as
    unrecognized at scaffold time.
    """

    mirror = (SHIPPED_TEMPLATE_ROOT / "backend" / "src" / "app.js").read_text(
        encoding="utf-8"
    ).replace("\r\n", "\n")

    assert mirror.count("    res.sendFile(path.join(frontendDistPath, 'index.html'));\n") == 1
    assert "dotfiles" not in mirror


def test_patch_dependencies_must_be_registered_in_order() -> None:
    """A dependent registered before its prerequisite is a broken registration.

    The dependent's search shapes assume the prerequisite's output; validating
    registration order turns a silent skip into an actionable error.
    """

    dependent = next(
        patch
        for patch in template_patches.TEMPLATE_PATCHES
        if patch.name == "init-db-rethrow-genuine-init-failures"
    )
    reordered = (dependent, *template_patches.TEMPLATE_PATCHES[:1])

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(template_patches, "TEMPLATE_PATCHES", reordered)
    try:
        with pytest.raises(ValueError, match="registered before its dependent"):
            template_patches.patches_for("web-react-express")
    finally:
        monkeypatched.undo()


def test_a_dependent_patch_is_unrecognized_when_its_prerequisite_fails(tmp_path) -> None:
    """The rethrow fix must not run on a bootstrap the base fix never reached.

    Its search shape only exists after the first patch applied, so an
    unrecognized prerequisite must make the dependent unrecognized as well -
    that is what stops a half-fixed bootstrap from compiling.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    bootstrap = workspace / "backend" / "src" / "database" / "init_db.js"
    bootstrap.write_text("// rewritten upstream\n", encoding="utf-8")

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        UNRECOGNIZED,
        UNRECOGNIZED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]
    assert "prerequisite" in outcomes[1].detail
    assert bootstrap.read_text(encoding="utf-8") == "// rewritten upstream\n"


def test_a_template_without_the_target_files_skips_instead_of_failing(tmp_path) -> None:
    """A template that never shipped init_db.js has nothing to patch.

    Skipped patches must not fail the scaffold - that template never carried
    the file the fix repairs - but a dependent of a skipped patch skips too.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        SKIPPED,
        SKIPPED,
        SKIPPED,
        SKIPPED,
        SKIPPED,
        SKIPPED,
    ]
    assert "no target files" in outcomes[0].detail
    assert "prerequisite" in outcomes[1].detail
    assert "no target files" in outcomes[2].detail


def test_post_template_setup_fails_when_a_patch_cannot_be_classified(tmp_path) -> None:
    """An unclassifiable target must fail the scaffold, not just log a warning.

    The fixes are load-bearing for every node's TDD loop; continuing would
    surface them as per-node test errors and a polluted run instead of one
    actionable startup failure.
    """

    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, templates_root / "web-react-express")
    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    bootstrap = workspace / "backend" / "src" / "database" / "init_db.js"
    bootstrap.write_text("// rewritten upstream\n", encoding="utf-8")
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setenv("ARC_AGENT_TEMPLATES_ROOT", str(templates_root))
    handler = _make_handler(workspace)
    messages: list[tuple] = []

    def recording_log(agent_name, message, status=None, node_id=None):
        messages.append((agent_name, message, status))

    handler.log_cb = recording_log

    try:
        assert asyncio.run(handler.post_template_setup()) is False
    finally:
        monkeypatched.undo()

    assert any(status == "error" and "did not apply cleanly" in message for _, message, status in messages)
    warnings = [message for _, message, status in messages if status == "warning"]
    assert warnings, "each unapplied patch must be logged as a warning before the abort"
    assert all("was not applied" in message for message in warnings)


def test_shipped_template_fixes_recognize_a_crlf_copy_that_already_has_them(tmp_path) -> None:
    """A CRLF template must be recognized as fixed, not reported as unknown.

    The markers are single lines, so their matching cannot depend on the file's
    line endings; a CRLF-only difference that turned an already-patched file
    into an unrecognized one would silently drop the fix on such templates.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    for name in (
        "backend/src/database/init_db.js",
        "backend/src/database/test_harness.js",
        "README.md",
        "backend/src/app.js",
    ):
        path = workspace / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("\n", "\r\n"),
            encoding="utf-8",
            newline="",
        )

    first = apply_template_patches(str(workspace), "web-react-express")
    assert [outcome.status for outcome in first] == [
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]

    second = apply_template_patches(str(workspace), "web-react-express")
    assert [outcome.status for outcome in second] == [
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
        ALREADY_APPLIED,
    ]


def test_fix_markers_are_single_lines_so_line_endings_cannot_hide_them() -> None:
    """`_classify` matches markers against the raw text, so they must be one line."""

    for patch in template_patches.TEMPLATE_PATCHES:
        for edit in patch.edits:
            assert "\n" not in edit.applied_marker


def test_shipped_template_fixes_refuse_a_half_repaired_file(tmp_path) -> None:
    """A file carrying both the fix marker and the pre-fix shape is ambiguous.

    This module never produces that state, so seeing it means something else
    edited the file. Guessing which of the two shapes is current is how a broken
    bootstrap would be reported as fixed.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    bootstrap = workspace / "backend" / "src" / "database" / "init_db.js"
    pre_fix_with_stray_marker = (
        bootstrap.read_text(encoding="utf-8") + "\nconst MAX_INIT_ATTEMPTS = 5;\n"
    )
    bootstrap.write_text(pre_fix_with_stray_marker, encoding="utf-8")

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        UNRECOGNIZED,
        UNRECOGNIZED,
        APPLIED,
        APPLIED,
        APPLIED,
        APPLIED,
    ]
    assert "init_db.js" in outcomes[0].detail
    assert bootstrap.read_text(encoding="utf-8") == pre_fix_with_stray_marker


def test_shipped_template_fixes_leave_no_trace_when_staging_fails(
    tmp_path, monkeypatch
) -> None:
    """A failure while producing content must leave every target as it was.

    The staged-write path must fail without touching a single target, and the
    dependent patch must not run against a prerequisite that did not resolve:
    both effects together are what keep a failed scaffold unwritten rather than
    half-fixed.
    """

    workspace = tmp_path / "workspace"
    shutil.copytree(SHIPPED_TEMPLATE_ROOT, workspace)
    before = _patched_file_contents(workspace)

    real_write = template_patches._write_text
    written: list[str] = []

    def flaky_write(path: str, text: str) -> None:
        written.append(path)
        # Fail the first staging write of every patch that reaches one: the
        # bootstrap chain (init_db.js), the harness guard (test_harness.js),
        # the gitignore entry (.gitignore) and both app.js patches (the SPA
        # fallback and the router mount guard). Staged paths carry the
        # `.arc-patch-tmp` suffix, hence the substring match.
        if any(
            name in os.path.basename(path)
            for name in ("init_db.js", "test_harness.js", ".gitignore", "app.js")
        ):
            raise OSError("disk full")
        real_write(path, text)

    monkeypatch.setattr(template_patches, "_write_text", flaky_write)

    outcomes = apply_template_patches(str(workspace), "web-react-express")

    assert [outcome.status for outcome in outcomes] == [
        UNRECOGNIZED,
        UNRECOGNIZED,
        UNRECOGNIZED,
        UNRECOGNIZED,
        UNRECOGNIZED,
        UNRECOGNIZED,
    ]
    assert _patched_file_contents(workspace) == before
    assert list(workspace.rglob("*.arc-patch-tmp")) == []

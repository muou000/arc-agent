"""Slow integration tests for the frontend portion of ``template/``.

Run with ``pytest -m slow tests/test_template_contract``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_FRONTEND = (
    REPO_ROOT / "arc-template" / "templates" / "web-react-express" / "frontend"
)


def _has_node() -> bool:
    return any(shutil.which(c) for c in ("node", "node.exe", "npm", "npm.cmd", "npm.ps1"))


_NODE_BIN = next((shutil.which(c) for c in ("node", "node.exe") if shutil.which(c)), "node")
_NPM_BIN = next((shutil.which(c) for c in ("npm", "npm.cmd", "npm.ps1") if shutil.which(c)), "npm")


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _has_node(), reason="node/npm not available on PATH"),
]


@pytest.fixture(scope="module")
def installed_frontend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Copy the frontend template and install the way production does.

    ``run_npm_install`` tries a plain ``npm install`` first and falls back to
    ``--legacy-peer-deps`` (npm 10's arborist crashes while resolving vitest's
    optional peers). The fallback skips peer installation, and the officially
    provisioned template does not declare ``@testing-library/dom`` - so the
    fixture ends with the same ``--no-save --no-package-lock`` patch the
    handler applies in ``_ensure_testing_library_dom``. Failures fail loudly:
    skipping is how the arborist crash once stayed invisible.
    """
    scratch = tmp_path_factory.mktemp("template-frontend-")
    shutil.copytree(TEMPLATE_FRONTEND, scratch / "frontend")

    def _npm(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [_NPM_BIN, "install", *args, "--no-audit", "--no-fund", "--prefer-offline"],
            cwd=str(scratch / "frontend"),
            capture_output=True,
            text=True,
            timeout=300,
        )

    proc = _npm([])
    if proc.returncode != 0:
        proc = _npm(["--legacy-peer-deps"])
    if proc.returncode != 0:
        pytest.fail(f"npm install failed: {proc.stderr or proc.stdout}")

    dom = scratch / "frontend" / "node_modules" / "@testing-library" / "dom"
    if not dom.is_dir():
        patch = _npm(["--no-save", "--no-package-lock", "@testing-library/dom@^10.4.0"])
        if patch.returncode != 0:
            pytest.fail(
                f"@testing-library/dom patch install failed: {patch.stderr or patch.stdout}"
            )
    yield scratch / "frontend"


class TestFrontendDependencies:
    """Peers that `--legacy-peer-deps` does not install for us."""

    def test_testing_library_dom_is_a_real_install(self, installed_frontend: Path) -> None:
        """`@testing-library/react` 16 peers on `@testing-library/dom`.

        The officially provisioned template does not declare the peer, and the
        `--legacy-peer-deps` fallback skips peer installation - production
        recovers through the handler's `--no-save` patch, which this fixture
        mirrors. Without that guarantee every component test fails to import,
        and the agent has no way to install a dependency mid-compile.
        """

        dom = installed_frontend / "node_modules" / "@testing-library" / "dom"
        assert dom.is_dir(), "@testing-library/dom is missing; component tests cannot import it"
        assert (dom / "dist").is_dir(), "@testing-library/dom looks like a stub, not a real install"


class TestFrontendBuild:
    def test_vite_build_produces_index_html(self, installed_frontend: Path) -> None:
        proc = subprocess.run(
            [_NPM_BIN, "run", "build"],
            cwd=str(installed_frontend),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        dist = installed_frontend / "dist"
        assert (dist / "index.html").is_file()


class TestFrontendStatic:
    """Pure static checks that do not require npm install."""

    def test_vite_config_present(self) -> None:
        assert (TEMPLATE_FRONTEND / "vite.config.js").is_file()

    def test_eslint_config_present(self) -> None:
        assert (TEMPLATE_FRONTEND / "eslint.config.js").is_file()

    def test_package_json_has_build_script(self) -> None:
        pkg = json.loads((TEMPLATE_FRONTEND / "package.json").read_text(encoding="utf-8"))
        assert "build" in pkg["scripts"]
        assert "test" in pkg["scripts"]
        assert "dev" in pkg["scripts"]

    def test_test_setup_file_present(self) -> None:
        # The setup file is referenced from vitest config and provides jest-dom matchers.
        setup = TEMPLATE_FRONTEND / "test" / "setup.ts"
        assert setup.is_file()
        text = setup.read_text(encoding="utf-8")
        assert "@testing-library/jest-dom" in text
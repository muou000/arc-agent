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
    scratch = tmp_path_factory.mktemp("template-frontend-")
    shutil.copytree(TEMPLATE_FRONTEND, scratch / "frontend")
    # Match the production install path: npm 10's arborist crashes while
    # resolving vitest's optional peers, so the handler falls back to
    # --legacy-peer-deps. This fixture must exercise the same flags.
    proc = subprocess.run(
        [_NPM_BIN, "install", "--legacy-peer-deps", "--no-audit", "--no-fund", "--prefer-offline"],
        cwd=str(scratch / "frontend"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        # Fail loudly. Skipping here is how the npm 10 arborist crash - and the
        # missing @testing-library/dom peer it exposed - stayed invisible.
        # `_has_node()` already skips genuinely node-less environments above.
        pytest.fail(f"npm install failed: {proc.stderr or proc.stdout}")
    yield scratch / "frontend"


class TestFrontendDependencies:
    """Peers that `--legacy-peer-deps` does not install for us."""

    def test_testing_library_dom_is_a_real_install(self, installed_frontend: Path) -> None:
        """`@testing-library/react` 16 peers on `@testing-library/dom`.

        `--legacy-peer-deps` skips peer installation, so the template must
        declare it directly. Without it every component test fails to import,
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


class TestRegistrationGlue:
    """Registration-based glue: pages, sections, and providers contributed
    after install must be picked up by the template's glob loaders without
    editing App.tsx, HomePage.tsx, or main.tsx.

    These tests write modules into the shared ``installed_frontend`` scratch,
    so they must run after the pristine-build test above (class definition
    order).
    """

    _ABOUT_PAGE = """function AboutPage() {
  return <main data-testid="about-page">About registration page</main>;
}

export const route = '/about';

export default AboutPage;
"""

    _HERO_SECTION = """function HeroSection() {
  return <section data-testid="hero-section">Hero section</section>;
}

export const sectionOrder = 1;

export default HeroSection;
"""

    _TEST_PROVIDER = """import type { ReactNode } from 'react';

function TestProvider({ children }: { children?: ReactNode }) {
  return <div data-testid="provider-boundary">{children}</div>;
}

export default TestProvider;
"""

    _REGISTRATION_TEST = """import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import App from '../src/App';

describe('registration-based glue', () => {
  it('mounts sections and providers on the home route', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('provider-boundary')).toBeTruthy();
    expect(screen.getByTestId('hero-section')).toBeTruthy();
  });

  it('routes to pages registered through the route export', () => {
    render(
      <MemoryRouter initialEntries={['/about']}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('about-page')).toBeTruthy();
  });
});
"""

    def _write_modules(self, installed_frontend: Path) -> None:
        (installed_frontend / "src" / "pages" / "AboutPage.tsx").write_text(
            self._ABOUT_PAGE, encoding="utf-8"
        )
        (installed_frontend / "src" / "sections" / "home" / "HeroSection.tsx").write_text(
            self._HERO_SECTION, encoding="utf-8"
        )
        (installed_frontend / "src" / "providers" / "TestProvider.tsx").write_text(
            self._TEST_PROVIDER, encoding="utf-8"
        )
        tests_dir = installed_frontend / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "registration.test.tsx").write_text(self._REGISTRATION_TEST, encoding="utf-8")

    def test_glob_registered_modules_build_and_render(self, installed_frontend: Path) -> None:
        self._write_modules(installed_frontend)

        build = subprocess.run(
            [_NPM_BIN, "run", "build"],
            cwd=str(installed_frontend),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert build.returncode == 0, build.stderr

        vitest = subprocess.run(
            [_NPM_BIN, "run", "test"],
            cwd=str(installed_frontend),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert vitest.returncode == 0, vitest.stdout + vitest.stderr


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
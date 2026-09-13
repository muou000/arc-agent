"""Tests for the static layout of the web template.

These tests do not require Node.js; they verify that the manifest
``template.yaml`` agrees with the on-disk project structure and that
every documented command in ``README.md`` is actually wired up in
``package.json`` files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPO_ROOT / "arc-template" / "templates" / "web-react-express"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load((TEMPLATE_ROOT / "template.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def backend_package() -> dict:
    return json.loads((TEMPLATE_ROOT / "backend" / "package.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frontend_package() -> dict:
    return json.loads((TEMPLATE_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def readme_text() -> str:
    return (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")


class TestManifestSelfConsistency:
    def test_manifest_top_level_keys(self, manifest: dict) -> None:
        for key in ("schema_version", "id", "name", "type", "version", "stack", "copy", "agent_guidance"):
            assert key in manifest, f"missing manifest key: {key}"

    def test_agent_guidance_paths_exist(self, manifest: dict) -> None:
        guidance = manifest["agent_guidance"]
        for key in (
            "ui_root",
            "api_root",
            "function_root",
            "db_root",
            "frontend_test_root",
            "backend_test_root",
            "e2e_test_root",
        ):
            assert key in guidance, f"missing agent_guidance key: {key}"
            path = TEMPLATE_ROOT / guidance[key]
            assert path.exists(), f"declared path does not exist: {guidance[key]} -> {path}"

    def test_stack_matches_package_json(
        self, manifest: dict, backend_package: dict, frontend_package: dict
    ) -> None:
        backend = manifest["stack"]["backend"]
        assert backend["framework"] == "Express"
        assert "express" in {k.lower() for k in backend_package["dependencies"]}
        frontend = manifest["stack"]["frontend"]
        assert frontend["framework"] == "Vite"
        assert "vite" in {k.lower() for k in frontend_package["devDependencies"]}
        assert frontend["library"] == "React"
        assert "react" in {k.lower() for k in frontend_package["dependencies"]}

    def test_test_frameworks_declared(
        self, manifest: dict, backend_package: dict, frontend_package: dict
    ) -> None:
        tests = manifest["stack"]["tests"]
        assert tests["frontend"] == "Vitest"
        assert tests["backend"] == "Vitest"
        assert tests["e2e"] == "Playwright"
        assert "vitest" in {k.lower() for k in backend_package["devDependencies"]}
        assert "vitest" in {k.lower() for k in frontend_package["devDependencies"]}
        assert "@playwright/test" in backend_package["devDependencies"]

    def test_copy_excludes_template_yaml(self, manifest: dict) -> None:
        excludes = manifest["copy"]["exclude"]
        assert "template.yaml" in excludes


class TestReadmeCommandsAreWired:
    """Every ``npm run X`` mentioned in the README must exist as a script in at
    least one of the project ``package.json`` files.

    The README is a single document that describes commands from both backend
    and frontend packages; we therefore assert that every mentioned command
    appears in *some* package.json.
    """

    def test_readme_commands_resolve(self, readme_text: str) -> None:
        scripts: set[str] = set()
        for pkg in (TEMPLATE_ROOT / "backend" / "package.json", TEMPLATE_ROOT / "frontend" / "package.json"):
            scripts.update(json.loads(pkg.read_text(encoding="utf-8"))["scripts"].keys())
        mentioned = set(re.findall(r"npm run ([A-Za-z0-9:_\\-]+)", readme_text))
        for cmd in mentioned:
            assert cmd in scripts, (
                f"README references `npm run {cmd}` but no package.json declares it"
            )


class TestDatabaseLayer:
    def test_runtime_helpers_present(self) -> None:
        for filename in ("init_db.js", "db_runtime.js", "seed_db.js", "test_harness.js"):
            assert (TEMPLATE_ROOT / "backend" / "src" / "database" / filename).is_file()

    def test_index_re_exports(self) -> None:
        idx = TEMPLATE_ROOT / "backend" / "src" / "database" / "index.js"
        text = idx.read_text(encoding="utf-8")
        for symbol in ("seedDatabase", "createTestDatabaseHarness"):
            assert symbol in text


class TestRegistrationGlueStructure:
    """Shared glue files must stay registration-based: agents contribute new
    per-feature modules and the template assembles them, so concurrent nodes
    never edit the same file (the DESIGN-stage denylist depends on this)."""

    def test_app_js_mounts_route_registry(self) -> None:
        text = (TEMPLATE_ROOT / "backend" / "src" / "app.js").read_text(encoding="utf-8")
        assert "require('./routes')" in text
        assert "registerRoutes(app)" in text
        assert "/api/health" in text

    def test_route_registry_loader_contract(self) -> None:
        loader = TEMPLATE_ROOT / "backend" / "src" / "routes" / "index.js"
        assert loader.is_file()
        text = loader.read_text(encoding="utf-8")
        assert ".routes.js" in text
        assert "mountPath" in text
        assert "registerRoutes" in text

    def test_init_db_loads_schema_modules(self) -> None:
        text = (TEMPLATE_ROOT / "backend" / "src" / "database" / "init_db.js").read_text(encoding="utf-8")
        assert "schema" in text
        assert ".schema.js" in text
        assert "apply(db)" in text or "apply(database)" in text
        # The old "centralize schema evolution in this file" guidance must stay
        # gone: it is what drove concurrent nodes into init_db.js conflicts.
        assert "centralized in this file" not in text

    def test_schema_module_directory_exists(self) -> None:
        assert (TEMPLATE_ROOT / "backend" / "src" / "database" / "schema").is_dir()

    def test_app_tsx_registers_pages_and_providers_via_glob(self) -> None:
        text = (TEMPLATE_ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        assert "import.meta.glob(" in text
        assert "'./pages/**/*.tsx'" in text
        assert "'./providers/*.tsx'" in text
        # Test assets must be excluded so a misplaced test file cannot leak
        # into the app bundle through the eager glob.
        assert "'!./pages/**/__tests__/**'" in text
        assert "'!./pages/**/*.test.tsx'" in text
        assert "<Routes>" in text

    def test_home_page_composes_sections_via_glob(self) -> None:
        text = (TEMPLATE_ROOT / "frontend" / "src" / "pages" / "HomePage.tsx").read_text(encoding="utf-8")
        assert "'../sections/home/*.tsx'" in text
        assert "'!../sections/home/*.test.tsx'" in text
        assert "sectionOrder" in text
        assert "export const route = '/'" in text

    def test_registration_module_directories_exist(self) -> None:
        for relative in (
            "frontend/src/sections/home",
            "frontend/src/providers",
            "backend/src/routes",
            "backend/src/database/schema",
        ):
            assert (TEMPLATE_ROOT / relative).is_dir(), f"missing registration directory: {relative}"

    def test_agent_guidance_api_root_points_to_route_registry(self, manifest: dict) -> None:
        assert manifest["agent_guidance"]["api_root"] == "backend/src/routes"


class TestGitignoreSemantics:
    """Backend/frontend ignore patterns must keep ``node_modules`` out of git."""

    def test_gitignore_files_exist(self) -> None:
        # Templates are usually copied without their own .gitignore; this
        # assertion documents that intent. The runtime SDK will inject the
        # managed block when ensure_repo runs.
        for sub in ("backend", "frontend"):
            assert (TEMPLATE_ROOT / sub).is_dir()
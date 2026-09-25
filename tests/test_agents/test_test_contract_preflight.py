from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents.runtime.test_contract_preflight import run_test_contract_preflight

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_PATH = _REPO_ROOT / "skills" / "web-test-harness-skill" / "SKILL.md"
_SKILL_VITEST_FORM = re.compile(r"`(import\s*\{[^}]+\}\s*from\s*'vitest')`")
_SKILL_PLAYWRIGHT_FORM = re.compile(r"`(const\s*\{[^}]+\}\s*=\s*require\('@playwright/test'\))`")


def _workspace(
    tmp_path: Path,
    *,
    test_path: str = "backend/tests/example.test.js",
    test_type: str = "Unit",
    test_content: str = "import { describe, it, expect } from 'vitest';\nit('works', () => expect(1).toBe(1));\n",
    config: str = """const { defineConfig } = require('vitest/config');
module.exports = defineConfig({
  test: {
    include: ['tests/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    globals: true,
  },
});
""",
    package: dict | None = None,
) -> tuple[Path, str]:
    root = tmp_path / "workspace"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / "vitest.config.js").write_text(config, encoding="utf-8")
    (backend / "package.json").write_text(
        json.dumps(
            package
            or {
                "name": "backend",
                "devDependencies": {"vitest": "^4.0.0", "@playwright/test": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    path = root / test_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(test_content, encoding="utf-8")
    return root, test_path


def _run(root: Path, test_path: str, test_type: str = "Unit"):
    return run_test_contract_preflight(
        root,
        app_type="web",
        tests=[{"test_id": "T1", "type": test_type, "file_path": test_path}],
    )


def _write_test_file(root: Path, test_path: str, content: str) -> None:
    path = root / test_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _dual_config_backend(tmp_path: Path) -> Path:
    """Mirror the web template: one CommonJS backend package with both runner configs."""

    root = tmp_path / "workspace"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / "package.json").write_text(
        json.dumps(
            {
                "name": "backend",
                "devDependencies": {"vitest": "^4.0.0", "@playwright/test": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (backend / "vitest.config.js").write_text(
        """const { defineConfig } = require('vitest/config');
module.exports = defineConfig({
  test: {
    include: ['tests/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    exclude: ['test-e2e/**/*'],
  },
});
""",
        encoding="utf-8",
    )
    (backend / "playwright.config.js").write_text(
        """const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({ testDir: './test-e2e', testMatch: /.*\\.(js|jsx|ts|tsx)$/ });
""",
        encoding="utf-8",
    )
    return root


def test_commonjs_runner_entry_is_blocked_in_esm_package(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        config="""import { defineConfig } from 'vitest/config';
export default defineConfig({ test: { include: ['tests/**/*.test.js'] } });
""",
        test_content="const { describe, it, expect } = require('vitest');\nit('works', () => expect(1).toBe(1));\n",
        package={"name": "backend", "type": "module", "devDependencies": {"vitest": "^4.0.0"}},
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    issue = next(issue for issue in report.issues if issue.kind == "commonjs_runner_entry")
    assert issue.classification == "deterministic"
    assert "type: module" in issue.message
    assert "ESM import" in issue.suggestion
    assert not report.can_start_tdd


def test_cjs_package_accepts_commonjs_playwright_entry(tmp_path: Path) -> None:
    root = _dual_config_backend(tmp_path)
    _write_test_file(
        root,
        "backend/test-e2e/login.e2e.spec.js",
        "const { test, expect } = require('@playwright/test');\ntest('works', async () => expect(true).toBeTruthy());\n",
    )

    report = _run(root, "backend/test-e2e/login.e2e.spec.js", "E2E")

    assert not report.deterministic_errors
    assert report.can_start_tdd


@pytest.mark.parametrize("e2e_first", [True, False], ids=["e2e-first", "vitest-first"])
def test_backend_dual_config_is_not_poisoned_by_manifest_order(tmp_path: Path, e2e_first: bool) -> None:
    root = _dual_config_backend(tmp_path)
    _write_test_file(
        root,
        "backend/test-e2e/register.e2e.spec.js",
        "const { test, expect } = require('@playwright/test');\ntest('works', async () => expect(true).toBeTruthy());\n",
    )
    _write_test_file(
        root,
        "backend/tests/authApi.test.js",
        "import { describe, it, expect } from 'vitest';\ndescribe('auth', () => { it('works', () => expect(1).toBe(1)); });\n",
    )
    _write_test_file(
        root,
        "backend/tests/authService.test.js",
        "import { describe, it, expect } from 'vitest';\ndescribe('service', () => { it('works', () => expect(1).toBe(1)); });\n",
    )
    e2e_entry = {"test_id": "T-E2E", "type": "E2E", "file_path": "backend/test-e2e/register.e2e.spec.js"}
    vitest_entries = [
        {"test_id": "T-1", "type": "Integration", "file_path": "backend/tests/authApi.test.js"},
        {"test_id": "T-2", "type": "Unit", "file_path": "backend/tests/authService.test.js"},
    ]
    tests = [e2e_entry, *vitest_entries] if e2e_first else [*vitest_entries, e2e_entry]

    report = run_test_contract_preflight(root, app_type="web", tests=tests)

    assert report.deterministic_errors == []
    assert report.can_start_tdd


def test_skill_canonical_runner_forms_pass_preflight(tmp_path: Path) -> None:
    skill_text = _SKILL_PATH.read_text(encoding="utf-8")
    vitest_form = _SKILL_VITEST_FORM.search(skill_text)
    playwright_form = _SKILL_PLAYWRIGHT_FORM.search(skill_text)
    assert vitest_form is not None, "web-test-harness-skill no longer teaches a canonical Vitest ESM import"
    assert playwright_form is not None, "web-test-harness-skill no longer teaches a canonical Playwright CJS require"

    root = _dual_config_backend(tmp_path)
    _write_test_file(
        root,
        "backend/tests/canonical.test.js",
        f"{vitest_form.group(1)};\nit('works', () => expect(1).toBe(1));\n",
    )
    _write_test_file(
        root,
        "backend/test-e2e/canonical.e2e.spec.js",
        f"{playwright_form.group(1)};\ntest('works', async () => expect(true).toBeTruthy());\n",
    )
    report = run_test_contract_preflight(
        root,
        app_type="web",
        tests=[
            {"test_id": "T-E2E", "type": "E2E", "file_path": "backend/test-e2e/canonical.e2e.spec.js"},
            {"test_id": "T-UNIT", "type": "Unit", "file_path": "backend/tests/canonical.test.js"},
        ],
    )

    assert report.deterministic_errors == []
    assert report.can_start_tdd


def test_esm_test_entry_rejects_commonjs_syntax(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_path="backend/tests/example.test.mjs",
        test_content="const { it } = require('vitest');\nit('works', () => {});\n",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    assert any(issue.kind == "module_syntax" for issue in report.issues)


def test_runner_config_missing_static_entry_is_deterministic(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        config="""const helper = require('./config/missing.js');
const { defineConfig } = require('vitest/config');
module.exports = defineConfig({ test: { include: ['tests/**/*.test.js'] }, helper });
""",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    assert any(issue.kind == "module_resolution" and issue.file_path == "backend/vitest.config.js" for issue in report.issues)


def test_bare_vitest_globals_are_blocked_when_config_disables_globals(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        config="""const { defineConfig } = require('vitest/config');
module.exports = defineConfig({
  test: { include: ['tests/**/*.test.js'], globals: false },
});
""",
        test_content="describe('suite', () => { beforeAll(() => {}); it('works', () => expect(1).toBe(1)); });\n",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    issue = next(issue for issue in report.issues if issue.kind == "runner_global")
    assert issue.classification == "deterministic"
    assert "beforeAll" in issue.message
    assert "expect" in issue.message


def test_bare_vitest_globals_are_blocked_when_config_omits_globals(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        config="""const { defineConfig } = require('vitest/config');
module.exports = defineConfig({ test: { include: ['tests/**/*.test.js'] } });
""",
        test_content="beforeAll(() => {}); it('works', () => expect(true).toBe(true));\n",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    assert any(issue.kind == "runner_global" for issue in report.issues)


def test_missing_relative_target_is_deterministic(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_content="import { it } from 'vitest';\nimport { missing } from '../../src/missing.js';\nit('works', () => missing());\n",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    issue = next(issue for issue in report.issues if issue.kind == "module_resolution")
    assert issue.classification == "deterministic"
    assert "missing.js" in issue.message


def test_dynamic_import_and_alias_fail_open(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_content="""import { it } from 'vitest';
const name = process.env.TEST_MODULE;
const loaded = await import(`./${name}.js`);
import { value } from '@/runtime/value';
it('works', () => loaded && value);
""",
    )

    report = _run(root, test_path)

    assert report.can_start_tdd
    assert report.status == "warning"
    assert any(issue.kind == "dynamic_module_resolution" for issue in report.issues)
    assert any(issue.kind == "bundler_alias" for issue in report.issues)
    assert not report.deterministic_errors


def test_playwright_entry_and_test_match_are_checked(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_path="backend/test-e2e/login.spec.js",
        test_type="E2E",
        test_content="import { test, expect } from '@playwright/test';\ntest('works', async () => expect(true).toBeTruthy());\n",
        config="""const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests',
  testMatch: /.*\\.test\\.js$/,
});
""",
    )
    (root / "backend" / "vitest.config.js").unlink()
    (root / "backend" / "playwright.config.js").write_text(
        """const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({ testDir: './tests', testMatch: /.*\\.test\\.js$/ });
""",
        encoding="utf-8",
    )

    report = _run(root, test_path, "E2E")

    assert report.status == "blocked"
    kinds = {issue.kind for issue in report.issues}
    assert "runner_entry" in kinds


def test_playwright_globals_without_config_fail_open(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_path="backend/test-e2e/login.spec.js",
        test_type="E2E",
        test_content="test('works', async () => expect(true).toBeTruthy());\n",
        package={"name": "backend", "devDependencies": {"@playwright/test": "^1.0.0"}},
    )
    (root / "backend" / "vitest.config.js").unlink()

    report = _run(root, test_path, "E2E")

    assert report.can_start_tdd
    assert report.status == "warning"
    assert not report.deterministic_errors


def test_missing_manifest_file_is_blocked_in_a_web_workspace(tmp_path: Path) -> None:
    root, test_path = _workspace(tmp_path)
    (root / test_path).unlink()

    report = _run(root, test_path)

    assert report.status == "blocked"
    assert any(issue.kind == "missing_test_file" for issue in report.issues)
    assert "no testdrivendeveloper session" in report.render().lower()


def test_python_faux_workspace_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    report = run_test_contract_preflight(
        root,
        app_type="web",
        tests=[{"test_id": "T1", "type": "Unit", "file_path": "tests/unit/test_calc.py"}],
    )

    assert report.status == "skipped"
    assert report.can_start_tdd is True
    assert report.issues == []

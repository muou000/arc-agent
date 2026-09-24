from __future__ import annotations

import json
from pathlib import Path

from agents.runtime.test_contract_preflight import run_test_contract_preflight


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


def test_commonjs_runner_entry_is_blocked_with_an_actionable_fix(tmp_path: Path) -> None:
    root, test_path = _workspace(
        tmp_path,
        test_content="const { describe, it, expect } = require('vitest');\nit('works', () => expect(1).toBe(1));\n",
    )

    report = _run(root, test_path)

    assert report.status == "blocked"
    issue = next(issue for issue in report.issues if issue.kind == "commonjs_runner_entry")
    assert issue.classification == "deterministic"
    assert "ESM import" in issue.suggestion
    assert not report.can_start_tdd


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

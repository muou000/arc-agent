"""Static, fail-closed checks for generated web-test contracts.

This module only checks facts available from the manifest and workspace files.
Bundler aliases, dynamic imports, and runtime-built config paths are warnings:
the actual runner remains authoritative for those cases.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agents.runtime.capabilities import normalize_manifest_path
from agents.runtime.import_checks import (
    classify_import,
    extract_relative_esm_imports,
    is_js_source_path,
    strip_js_comments,
)

PreflightClassification = Literal["deterministic", "environment", "runtime"]
PreflightStatus = Literal["passed", "warning", "blocked", "environment", "skipped"]

_RUNNER_GLOBALS = ("describe", "it", "beforeAll", "afterAll", "beforeEach", "afterEach", "expect", "test")
_ESM_ENTRY_SUFFIXES = frozenset({".mjs", ".mts"})
_CJS_ENTRY_SUFFIXES = frozenset({".cjs", ".cts"})
_JS_CONFIG_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
_VITEST_CONFIG_NAMES = tuple(f"vitest.config{ext}" for ext in _JS_CONFIG_EXTENSIONS) + tuple(
    f"vite.config{ext}" for ext in _JS_CONFIG_EXTENSIONS
)
_PLAYWRIGHT_CONFIG_NAMES = tuple(f"playwright.config{ext}" for ext in _JS_CONFIG_EXTENSIONS)
_RELATIVE_REQUIRE = re.compile(r"\brequire\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)")
_OPAQUE_REQUIRE = re.compile(r"\brequire\s*\(\s*(?!['\"])")
_STATIC_SPECIFIER = re.compile(r"(?:from\s*|import\s*|require\s*\(\s*)['\"]([^'\"\n]+)['\"]")
_NAMED_IMPORT = re.compile(
    r"\bimport\s*\{(?P<names>[^}]*)\}\s*from\s*['\"](?P<module>[^'\"]+)['\"]"
)
_REQUIRE_DESTRUCTURE = re.compile(
    r"\b(?:const|let|var)\s*\{(?P<names>[^}]*)\}\s*=\s*require\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)"
)
_ESM_SYNTAX = re.compile(
    r"\b(?:import\s+(?!\()|export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|\{|\*))"
)
_CJS_SYNTAX = re.compile(r"\b(?:require\s*\(|module\.exports\b|exports\.[A-Za-z_$])")


@dataclass(frozen=True)
class PreflightIssue:
    classification: PreflightClassification
    kind: str
    message: str
    file_path: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, str]:
        result = {
            "classification": self.classification,
            "kind": self.kind,
            "message": self.message,
            "file_path": self.file_path,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result


@dataclass
class TestContractPreflightReport:
    applicable: bool
    issues: list[PreflightIssue] = field(default_factory=list)
    checked_files: list[str] = field(default_factory=list)

    @property
    def deterministic_errors(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.classification == "deterministic"]

    @property
    def environment_errors(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.classification == "environment"]

    @property
    def runtime_warnings(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.classification == "runtime"]

    @property
    def primary_classification(self) -> str:
        if self.deterministic_errors:
            return "deterministic"
        if self.environment_errors:
            return "environment"
        if self.runtime_warnings:
            return "runtime"
        return ""

    @property
    def status(self) -> PreflightStatus:
        if not self.applicable:
            return "skipped"
        if self.deterministic_errors:
            return "blocked"
        if self.environment_errors:
            return "environment"
        return "warning" if self.runtime_warnings else "passed"

    @property
    def can_start_tdd(self) -> bool:
        return not self.applicable or (not self.deterministic_errors and not self.environment_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "status": self.status,
            "classification": self.primary_classification,
            "can_start_tdd": self.can_start_tdd,
            "checked_files": list(self.checked_files),
            "issues": [issue.to_dict() for issue in self.issues],
            "deterministic_error_count": len(self.deterministic_errors),
            "environment_error_count": len(self.environment_errors),
            "runtime_warning_count": len(self.runtime_warnings),
        }

    def render(self) -> str:
        if not self.applicable:
            return "Test contract preflight skipped: no materialized web JavaScript/TypeScript surface was found."
        if not self.issues:
            return "Test contract preflight passed; the TestDrivenDeveloper session may start."
        if self.deterministic_errors:
            headline = "Test contract preflight blocked TDD. No TestDrivenDeveloper session or run_tests budget was started."
        elif self.environment_errors:
            headline = "Test contract preflight found an environment failure. No TestDrivenDeveloper session or run_tests budget was started."
        else:
            headline = "Test contract preflight passed with runtime-only warnings; the runner remains authoritative."
        lines = [headline]
        for issue in self.issues:
            location = f" [{issue.file_path}]" if issue.file_path else ""
            suggestion = f" Suggestion: {issue.suggestion}" if issue.suggestion else ""
            lines.append(f"- [{issue.classification}:{issue.kind}]{location} {issue.message}{suggestion}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _TestAsset:
    manifest_path: str
    file_path: Path | None
    package_root: Path
    package_relative_path: str
    test_type: str


@dataclass(frozen=True)
class _PackageInfo:
    root: Path
    payload: dict[str, Any] | None
    present: bool


def run_test_contract_preflight(
    workspace_root: str | Path,
    *,
    app_type: str,
    tests: list[dict[str, Any]],
) -> TestContractPreflightReport:
    """Check provably broken web test contracts without starting a runner."""

    root = Path(workspace_root).expanduser().resolve()
    normalized_tests = [_normalize_test(test) for test in tests if isinstance(test, dict)]
    if str(app_type or "").strip().lower() not in {"web", "webapp", "web-app"}:
        return TestContractPreflightReport(applicable=False)
    web_marker = _has_web_marker(root)
    has_js_manifest = any(
        is_js_source_path(test["manifest_path"])
        for test in normalized_tests
        if test["manifest_path"]
    )
    if not has_js_manifest and not web_marker:
        return TestContractPreflightReport(applicable=False)

    report = TestContractPreflightReport(applicable=True)
    assets = [_describe_asset(root, test) for test in normalized_tests if test["manifest_path"]]
    report.checked_files = [asset.manifest_path for asset in assets]
    issues: list[PreflightIssue] = []
    packages: dict[Path, _PackageInfo] = {}
    contents: dict[Path, str] = {}

    for asset in assets:
        if not is_js_source_path(asset.manifest_path):
            continue
        if asset.file_path is None:
            classification: PreflightClassification = "deterministic" if web_marker else "runtime"
            kind = "missing_test_file" if web_marker else "unmaterialized_test_file"
            message = (
                "The test manifest points at a file that does not exist in the workspace."
                if web_marker
                else "The declared JavaScript test is not materialized yet; the runner will decide whether it is recoverable."
            )
            issues.append(
                PreflightIssue(
                    classification,
                    kind,
                    message,
                    asset.manifest_path,
                    "Create the declared test file or remove this manifest entry before TDD starts."
                    if web_marker
                    else "Let the runner validate the generated file at runtime.",
                )
            )
            continue

        package = packages.setdefault(asset.package_root, _load_package(asset.package_root))
        config_path, config_kind = _find_config(asset.package_root, asset.test_type)
        if package.payload is None:
            issues.extend(_package_issues(package, web_marker, asset))
        try:
            content = contents.setdefault(asset.file_path, asset.file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(
                PreflightIssue(
                    "environment",
                    "test_file_read",
                    f"The test file could not be read before the runner started: {exc}.",
                    asset.manifest_path,
                    "Make the file readable as UTF-8 and rerun the compile.",
                )
            )
            continue

        package_type = _package_type(package.payload)
        issues.extend(_check_module_syntax(asset, content, package_type, config=False))
        issues.extend(_check_runner_entry(asset, content, package_type))
        issues.extend(_check_static_modules(root, asset, content))
        issues.extend(_check_dynamic_resolution(asset, content))
        issues.extend(_check_runner_globals(asset, content, config_path, config_kind))
        issues.extend(_check_package_runner_dependency(asset, package))

        if config_path is None:
            issues.append(
                PreflightIssue(
                    "runtime",
                    "missing_runner_config",
                    "No Vitest or Playwright config was found for this test file; runtime defaults or generated config may still load it.",
                    asset.manifest_path,
                )
            )
            continue
        try:
            config_content = contents.setdefault(config_path, config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(
                PreflightIssue(
                    "environment",
                    "config_read",
                    f"The runner config could not be read: {exc}.",
                    _relative_path(root, config_path),
                )
            )
            continue
        config_asset = _asset_for_config(root, config_path, asset)
        issues.extend(_check_module_syntax(config_asset, config_content, package_type, config=True))
        issues.extend(_check_config_contract(asset, config_content))
        issues.extend(_check_static_modules(root, config_asset, config_content))
        issues.extend(_check_dynamic_resolution(config_asset, config_content, config=True))

    report.issues = _deduplicate_issues(issues)
    return report


def preflight_test_contract(
    workspace_root: str | Path,
    tests: list[dict[str, Any]],
    *,
    app_type: str = "web",
) -> TestContractPreflightReport:
    """Alias with the noun-first name used by external callers."""

    return run_test_contract_preflight(workspace_root, app_type=app_type, tests=tests)


def _normalize_test(test: dict[str, Any]) -> dict[str, str]:
    return {
        "manifest_path": normalize_manifest_path(test.get("file_path") or ""),
        "test_type": str(test.get("type") or "Unit").strip() or "Unit",
    }


def _has_web_marker(root: Path) -> bool:
    candidates = [root / "backend" / "package.json", root / "frontend" / "package.json"]
    for directory in (root, root / "backend", root / "frontend"):
        candidates.extend(directory / name for name in _VITEST_CONFIG_NAMES + _PLAYWRIGHT_CONFIG_NAMES)
    return any(path.exists() for path in candidates)


def _describe_asset(root: Path, test: dict[str, str]) -> _TestAsset:
    manifest_path = test["manifest_path"]
    package_root = _package_root(root, manifest_path, test["test_type"])
    file_path = _safe_workspace_path(root, manifest_path)
    return _TestAsset(
        manifest_path=manifest_path,
        file_path=file_path if file_path is not None and file_path.is_file() else None,
        package_root=package_root,
        package_relative_path=_relative_to_package(manifest_path, package_root, root),
        test_type=test["test_type"],
    )


def _package_root(root: Path, manifest_path: str, test_type: str) -> Path:
    normalized = manifest_path.replace("\\", "/")
    if normalized.startswith("frontend/"):
        return root / "frontend"
    if normalized.startswith("backend/"):
        return root / "backend"
    if str(test_type).strip().lower() == "e2e" and (root / "backend").exists():
        return root / "backend"
    if (root / "backend" / "package.json").exists() and (root / "frontend").exists():
        return root / "backend"
    return root


def _relative_to_package(manifest_path: str, package_root: Path, root: Path) -> str:
    normalized = manifest_path.replace("\\", "/")
    package_name = package_root.name if package_root != root else ""
    return normalized[len(package_name) + 1 :] if package_name and normalized.startswith(package_name + "/") else normalized


def _safe_workspace_path(root: Path, manifest_path: str) -> Path | None:
    normalized = normalize_manifest_path(manifest_path).replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return None
    candidate = posixpath.normpath(normalized)
    if candidate in {".", ".."} or candidate.startswith("../"):
        return None
    resolved = (root / Path(*candidate.split("/"))).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _load_package(package_root: Path) -> _PackageInfo:
    path = package_root / "package.json"
    if not path.is_file():
        return _PackageInfo(package_root, None, False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _PackageInfo(package_root, None, True)
    return _PackageInfo(package_root, payload if isinstance(payload, dict) else None, True)


def _package_issues(package: _PackageInfo, web_marker: bool, asset: _TestAsset) -> list[PreflightIssue]:
    package_path = _relative_path(asset.package_root.parent, asset.package_root / "package.json")
    if package.present:
        return [
            PreflightIssue(
                "deterministic",
                "invalid_package_json",
                "The package.json for the test runner is missing, unreadable, or not a JSON object.",
                package_path,
                "Repair package.json before starting TDD.",
            )
        ]
    if web_marker:
        return [
            PreflightIssue(
                "environment",
                "missing_package_json",
                "The test package has no package.json, so the configured runner environment cannot be verified.",
                package_path,
                "Materialize the package manifest and install the runner dependencies.",
            )
        ]
    return [
        PreflightIssue(
            "runtime",
            "unverified_package",
            "No package.json was found for this test file; dependency and config checks remain fail-open.",
            asset.manifest_path,
        )
    ]


def _package_type(payload: dict[str, Any] | None) -> str:
    return "module" if isinstance(payload, dict) and payload.get("type") == "module" else "commonjs"


def _find_config(package_root: Path, test_type: str) -> tuple[Path | None, str]:
    is_e2e = str(test_type).strip().lower() == "e2e"
    names = _PLAYWRIGHT_CONFIG_NAMES if is_e2e else _VITEST_CONFIG_NAMES
    for name in names:
        path = package_root / name
        if path.is_file():
            return path, "playwright" if is_e2e else "vitest"
    return None, "playwright" if is_e2e else "vitest"


def _asset_for_config(root: Path, config_path: Path, test_asset: _TestAsset) -> _TestAsset:
    relative = _relative_path(root, config_path)
    return _TestAsset(
        manifest_path=relative,
        file_path=config_path,
        package_root=test_asset.package_root,
        package_relative_path=_relative_to_package(relative, test_asset.package_root, root),
        test_type=test_asset.test_type,
    )


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _deduplicate_issues(issues: list[PreflightIssue]) -> list[PreflightIssue]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[PreflightIssue] = []
    for issue in issues:
        key = (issue.classification, issue.kind, issue.file_path, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def _check_module_syntax(
    asset: _TestAsset, content: str, package_type: str, *, config: bool
) -> list[PreflightIssue]:
    code = strip_js_comments(content)
    has_esm = bool(_ESM_SYNTAX.search(code))
    has_cjs = bool(_CJS_SYNTAX.search(code))
    if not has_esm and not has_cjs:
        return []

    suffix = Path(asset.manifest_path).suffix.lower()
    strict_kind: str | None = None
    if suffix in _ESM_ENTRY_SUFFIXES:
        strict_kind = "esm"
    elif suffix in _CJS_ENTRY_SUFFIXES:
        strict_kind = "cjs"
    elif config:
        strict_kind = "esm" if package_type == "module" else "cjs"

    if has_esm and has_cjs:
        return [
            PreflightIssue(
                "deterministic",
                "mixed_module_syntax",
                "The file mixes ESM and CommonJS entry syntax, so the selected runner cannot load it reliably.",
                asset.manifest_path,
                "Use one module style consistently: ESM import/export or CommonJS require/module.exports.",
            )
        ]
    if strict_kind == "esm" and has_cjs:
        return [
            PreflightIssue(
                "deterministic",
                "module_syntax",
                "The ESM entry contains CommonJS syntax.",
                asset.manifest_path,
                "Replace require/module.exports with import/export, or rename/configure the entry as CommonJS.",
            )
        ]
    if strict_kind == "cjs" and has_esm:
        return [
            PreflightIssue(
                "deterministic",
                "module_syntax",
                "The CommonJS entry contains ESM import/export syntax.",
                asset.manifest_path,
                "Replace import/export with require/module.exports, or use an ESM entry extension/configuration.",
            )
        ]
    return []


def _check_runner_entry(asset: _TestAsset, content: str, package_type: str) -> list[PreflightIssue]:
    code = strip_js_comments(content)
    expected = "@playwright/test" if asset.test_type.strip().lower() == "e2e" else "vitest"
    issues: list[PreflightIssue] = []
    for module, import_style in _runner_modules(code):
        if module not in {"vitest", "@playwright/test"}:
            continue
        if module != expected:
            issues.append(
                PreflightIssue(
                    "deterministic",
                    "runner_entry",
                    f"This {asset.test_type} test imports `{module}`, but the web handler runs it with `{expected}`.",
                    asset.manifest_path,
                    f"Import the test API from `{expected}` for this manifest type.",
                )
            )
        elif import_style == "commonjs":
            esm_reason = _esm_context_reason(asset, package_type)
            if esm_reason is None:
                continue
            issues.append(
                PreflightIssue(
                    "deterministic",
                    "commonjs_runner_entry",
                    f"The test loads `{module}` through CommonJS require, but {esm_reason}, so require() is not available.",
                    asset.manifest_path,
                    f"Use a named ESM import from `{module}` instead of require().",
                )
            )
    return issues


def _esm_context_reason(asset: _TestAsset, package_type: str) -> str | None:
    """Decide whether CommonJS require is provably unavailable for this entry.

    Shares the entry-extension vocabulary with `_check_module_syntax`'s
    strict_kind (`.mjs`/`.mts` are ESM, `.cjs`/`.cts` are CommonJS) but, for
    an ambiguous `.js`/`.ts` entry, also treats a `type: module` package as
    ESM; a CommonJS package may load its runner through require.
    """

    suffix = Path(asset.manifest_path).suffix.lower()
    if suffix in _ESM_ENTRY_SUFFIXES:
        return f"the `{suffix}` entry is ESM"
    if suffix in _CJS_ENTRY_SUFFIXES:
        return None
    if package_type == "module":
        return "the package is configured as ESM (`type: module`)"
    return None


def _runner_modules(code: str) -> list[tuple[str, Literal["esm", "commonjs"]]]:
    found: list[tuple[str, Literal["esm", "commonjs"]]] = []
    for match in _NAMED_IMPORT.finditer(code):
        found.append((match.group("module"), "esm"))
    for match in _REQUIRE_DESTRUCTURE.finditer(code):
        found.append((match.group("module"), "commonjs"))
    for match in _RELATIVE_REQUIRE.finditer(code):
        specifier = match.group(2)
        if specifier in {"vitest", "@playwright/test"}:
            found.append((specifier, "commonjs"))
    return found


def _check_static_modules(root: Path, asset: _TestAsset, content: str) -> list[PreflightIssue]:
    code = strip_js_comments(content)
    issues: list[PreflightIssue] = []
    importer_dir = posixpath.dirname(asset.manifest_path)

    def exists(path: str) -> bool:
        resolved = _safe_workspace_path(root, path)
        return resolved is not None and resolved.is_file()

    esm_imports, _opaque_dynamic = extract_relative_esm_imports(code)
    for specifier in esm_imports:
        violation = classify_import(specifier, importer_dir, exists)
        if violation is None or violation.kind in {"extension", "depth"}:
            continue
        issues.append(
            PreflightIssue(
                "deterministic",
                "module_resolution",
                f"The relative ESM import `{specifier}` resolves to `{violation.resolved}`, where no workspace file exists.",
                asset.manifest_path,
                "Create the target module or correct the relative import path.",
            )
        )

    for match in _RELATIVE_REQUIRE.finditer(code):
        specifier = match.group(2)
        if not specifier.startswith(("./", "../")):
            continue
        violation = classify_import(specifier, importer_dir, exists)
        if violation is None or violation.kind == "extension":
            continue
        issues.append(
            PreflightIssue(
                "deterministic",
                "module_resolution",
                f"The relative CommonJS import `{specifier}` resolves to `{violation.resolved}`, where no workspace file exists.",
                asset.manifest_path,
                "Create the target module or correct the relative require path.",
            )
        )
    return issues


def _check_dynamic_resolution(
    asset: _TestAsset, content: str, *, config: bool = False
) -> list[PreflightIssue]:
    del config
    code = strip_js_comments(content)
    issues: list[PreflightIssue] = []
    _static, opaque_dynamic = extract_relative_esm_imports(code)
    opaque_requires = len(_OPAQUE_REQUIRE.findall(code))
    if opaque_dynamic or opaque_requires:
        issues.append(
            PreflightIssue(
                "runtime",
                "dynamic_module_resolution",
                "A dynamic import/require path cannot be resolved without executing the runner; the preflight leaves it fail-open.",
                asset.manifest_path,
            )
        )
    aliases = sorted(
        {
            specifier
            for specifier in _STATIC_SPECIFIER.findall(code)
            if specifier.startswith(("@/", "~/", "#/"))
        }
    )
    if aliases:
        issues.append(
            PreflightIssue(
                "runtime",
                "bundler_alias",
                "The file uses a bundler alias that cannot be resolved from the workspace tree.",
                asset.manifest_path,
                "Keep the alias if the active Vitest/Vite/Playwright configuration resolves it; the runner will verify it.",
            )
        )
    return issues


def _check_runner_globals(
    asset: _TestAsset,
    content: str,
    config_path: Path | None,
    config_kind: str,
) -> list[PreflightIssue]:
    code = strip_js_comments(content)
    module = "@playwright/test" if config_kind == "playwright" else "vitest"
    imported = _imported_names(code, module)
    missing = _bare_runner_globals(code, imported, config_kind)
    if not missing:
        return []
    if config_path is None:
        return [
            PreflightIssue(
                "runtime",
                "unknown_runner_globals",
                f"The test uses bare runner globals ({', '.join(missing)}), but no runner config was found to prove how they are provided.",
                asset.manifest_path,
                "Let the active Playwright/Vitest runner decide whether globals are available.",
            )
        ]
    if config_kind == "playwright":
        return [
            PreflightIssue(
                "deterministic",
                "runner_global",
                f"The Playwright test uses bare runner globals ({', '.join(missing)}) without importing them.",
                asset.manifest_path,
                "Import `test` and `expect` from `@playwright/test` and use `test.describe` for suites.",
            )
        ]
    try:
        config_content = strip_js_comments(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    globals_match = re.search(r"\bglobals\s*:\s*(true|false)\b", config_content)
    if globals_match and globals_match.group(1) == "true":
        return []
    if globals_match and globals_match.group(1) == "false":
        return [
            PreflightIssue(
                "deterministic",
                "runner_global",
                f"Vitest globals are disabled, but the test uses bare globals ({', '.join(missing)}).",
                asset.manifest_path,
                "Import these names from `vitest`, or enable `test.globals` in the Vitest config.",
            )
        ]
    if re.search(r"\bglobals\s*:", config_content):
        return [
            PreflightIssue(
                "runtime",
                "unknown_runner_globals",
                f"The test uses bare runner globals ({', '.join(missing)}), but the Vitest globals setting is dynamic.",
                asset.manifest_path,
                "Resolve the globals setting or import these names from `vitest`.",
            )
        ]
    return [
        PreflightIssue(
            "deterministic",
            "runner_global",
            f"Vitest globals are not enabled, but the test uses bare globals ({', '.join(missing)}).",
            asset.manifest_path,
            "Import these names from `vitest`, or enable `test.globals: true` in the Vitest config.",
        )
    ]


def _imported_names(code: str, module: str) -> set[str]:
    names: set[str] = set()
    for pattern in (_NAMED_IMPORT, _REQUIRE_DESTRUCTURE):
        for match in pattern.finditer(code):
            if match.group("module") != module:
                continue
            for item in match.group("names").split(","):
                token = item.strip()
                if token:
                    names.add(re.split(r"\s+as\s+", token)[-1].strip())
    return names


def _bare_runner_globals(code: str, imported: set[str], config_kind: str) -> list[str]:
    candidates = ("test", "expect", "describe") if config_kind == "playwright" else _RUNNER_GLOBALS
    code_without_imports = re.sub(r"\bimport\b[\s\S]*?;", " ", code)
    return [
        name
        for name in candidates
        if name not in imported and re.search(rf"(?<![\w.$]){re.escape(name)}\s*\(", code_without_imports)
    ]


def _check_package_runner_dependency(asset: _TestAsset, package: _PackageInfo) -> list[PreflightIssue]:
    if package.payload is None:
        return []
    expected = "@playwright/test" if asset.test_type.strip().lower() == "e2e" else "vitest"
    declared: set[str] = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = package.payload.get(section)
        if isinstance(values, dict):
            declared.update(str(name) for name in values)
    if expected in declared:
        return []
    return [
        PreflightIssue(
            "environment",
            "missing_runner_dependency",
            f"The package manifest does not declare the runner dependency `{expected}` for this test type.",
            _relative_path(package.root.parent, package.root / "package.json"),
            f"Declare `{expected}` in dependencies or devDependencies and install it before TDD.",
        )
    ]


def _check_config_contract(asset: _TestAsset, content: str) -> list[PreflightIssue]:
    if asset.test_type.strip().lower() == "e2e":
        return _check_playwright_config(asset, content)
    return _check_vitest_config(asset, content)


def _check_vitest_config(asset: _TestAsset, code: str) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    relative_test = asset.package_relative_path
    include = _extract_literal_array(code, "include")
    exclude = _extract_literal_array(code, "exclude")
    if include is None and re.search(r"\binclude\s*:", code):
        issues.append(_runtime_config_issue(asset, "Vitest include is dynamic or not a literal array."))
    if exclude is None and re.search(r"\bexclude\s*:", code):
        issues.append(_runtime_config_issue(asset, "Vitest exclude is dynamic or not a literal array."))
    if include and not any(_glob_matches(pattern, relative_test) for pattern in include):
        issues.append(
            PreflightIssue(
                "deterministic",
                "runner_entry",
                f"Vitest include patterns do not select the manifest test `{relative_test}`.",
                asset.manifest_path,
                "Update the Vitest include pattern or move the test into its configured test directory.",
            )
        )
    if exclude and any(_glob_matches(pattern, relative_test) for pattern in exclude):
        issues.append(
            PreflightIssue(
                "deterministic",
                "runner_entry",
                f"Vitest exclude patterns exclude the manifest test `{relative_test}`.",
                asset.manifest_path,
                "Remove the matching exclude pattern or change the test placement.",
            )
        )
    setup_files = _extract_literal_array(code, "setupFiles")
    if setup_files is None and re.search(r"\bsetupFiles\s*:", code):
        setup_file = _extract_property_string(code, "setupFiles")
        if setup_file is not None:
            setup_files = [setup_file]
        else:
            issues.append(_runtime_config_issue(asset, "Vitest setupFiles is dynamic or not a literal path list."))
    for setup_file in setup_files or []:
        resolved = _safe_workspace_path(asset.package_root, setup_file)
        if resolved is None or not resolved.is_file():
            issues.append(
                PreflightIssue(
                    "deterministic",
                    "config_entry",
                    f"Vitest setupFiles entry `{setup_file}` does not resolve to a workspace file.",
                    asset.manifest_path,
                    "Create the setup file or correct the config path.",
                )
            )
    return issues


def _check_playwright_config(asset: _TestAsset, code: str) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    test_dir = _extract_property_string(code, "testDir")
    if test_dir is None and re.search(r"\btestDir\s*:", code):
        issues.append(_runtime_config_issue(asset, "Playwright testDir is dynamic."))
    elif test_dir and not _path_is_under_relative(asset.package_relative_path, test_dir):
        issues.append(
            PreflightIssue(
                "deterministic",
                "runner_entry",
                f"Playwright testDir `{test_dir}` does not contain the manifest test `{asset.package_relative_path}`.",
                asset.manifest_path,
                "Move the test under testDir or update the Playwright config.",
            )
        )

    test_match = _extract_regex_or_string(code, "testMatch")
    if test_match is None and re.search(r"\btestMatch\s*:", code):
        issues.append(_runtime_config_issue(asset, "Playwright testMatch is dynamic."))
    elif test_match and not _pattern_matches(test_match, asset.package_relative_path):
        issues.append(
            PreflightIssue(
                "deterministic",
                "runner_entry",
                f"Playwright testMatch does not select the manifest test `{asset.package_relative_path}`.",
                asset.manifest_path,
                "Update testMatch or move the test into the configured E2E shape.",
            )
        )
    return issues


def _runtime_config_issue(asset: _TestAsset, message: str) -> PreflightIssue:
    return PreflightIssue("runtime", "dynamic_runner_config", message, asset.manifest_path)


def _extract_literal_array(code: str, key: str) -> list[str] | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*\[(?P<body>[\s\S]*?)\]", code)
    if not match:
        return None
    body = match.group("body")
    if re.search(r"(?:=>|\b(?:function|const|let|var)\b|\$\{|process\.)", body):
        return None
    return re.findall(r"['\"]([^'\"]+)['\"]", body)


def _extract_property_string(code: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*(['\"])(?P<value>[^'\"]+)\1", code)
    return match.group("value") if match else None


def _extract_regex_or_string(code: str, key: str) -> str | None:
    string_value = _extract_property_string(code, key)
    if string_value is not None:
        return string_value
    match = re.search(rf"\b{re.escape(key)}\s*:\s*/(?P<body>(?:\\.|[^/])*)/[gimsuy]*", code)
    return f"/{match.group('body')}/" if match else None


def _glob_matches(pattern: str, path: str) -> bool:
    for expanded in _expand_braces(pattern):
        normalized = expanded.replace("\\", "/")
        if _glob_match_one(path, normalized) or _glob_match_one(path, normalized.lstrip("./")):
            return True
    return False


def _glob_match_one(path: str, pattern: str) -> bool:
    """Match the small glob subset used by Vitest/Playwright configs."""

    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*/", r"(?:.*/)?")
    escaped = escaped.replace(r"\*\*", r".*")
    escaped = escaped.replace(r"\*", r"[^/]*")
    escaped = escaped.replace(r"\?", r"[^/]")
    return re.fullmatch(escaped, path) is not None


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    return [
        expanded
        for value in match.group(1).split(",")
        for expanded in _expand_braces(pattern[: match.start()] + value + pattern[match.end() :])
    ]


def _pattern_matches(pattern: str, path: str) -> bool:
    if pattern.startswith("/") and pattern.endswith("/"):
        try:
            return bool(re.search(pattern[1:-1], path))
        except re.error:
            return False
    return _glob_matches(pattern, path)


def _path_is_under_relative(path: str, directory: str) -> bool:
    normalized_path = posixpath.normpath(path.replace("\\", "/"))
    normalized_dir = posixpath.normpath(directory.replace("\\", "/").lstrip("./"))
    return normalized_path == normalized_dir or normalized_path.startswith(normalized_dir.rstrip("/") + "/")

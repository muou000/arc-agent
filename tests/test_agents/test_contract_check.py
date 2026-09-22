"""Unit tests for the static test-contract satisfiability check.

``agents/tools/test_contract_check.py`` extracts the observable hooks a
generated E2E/Integration test drives (Playwright selectors, URLs, API
calls), classifies them against the requirement + interface-spec texts, and
declares the un-grounded remainder as test-contract hooks for the
implementation. Fixtures mirror the shapes observed on the test1 benchmark
run (Chinese accessible names, regex role names, supertest API paths).
"""

from __future__ import annotations

from pathlib import Path

from agents.tools.test_contract_check import (
    build_satisfiability_universe,
    classify_test_hooks,
    collect_manifest_hooks,
    extract_test_hooks,
    format_test_contract_context,
)


E2E_SPEC = """\
const { test, expect } = require('@playwright/test');

test('register flow', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Register' })).toBeVisible();
  await page.getByRole('link', { name: 'Register' }).click();
  await expect(page).toHaveURL(/\\/register$/);

  await page.getByLabel('用户名').fill('tb-user-abc');
  await page.getByLabel('登录密码').fill('Valid-password-123!');
  await page.getByPlaceholder('再次输入密码').fill('Valid-password-123!');
  await page.getByTestId('submit-btn').click();
  await expect(page.getByRole('button', { name: /下一步|提交/ })).toBeVisible();
  await expect(page.getByText('Sign out')).toBeVisible();

  const me = await page.request.get('/api/auth/me');
  expect(me.status()).toBe(200);
});
"""

INTEGRATION_SPEC = """\
const request = require('supertest');
const app = require('../src/app');

it('logs out', async () => {
  const agent = request.agent(app);
  await agent.post('/api/auth/register').send({});
  await agent.post('/api/auth/logout');
});
"""

UNIT_SPEC = """\
const { add } = require('../src/calc');
it('adds', () => expect(add(1, 1)).toBe(2));
"""

REQ_DATA = {
    "name": "注册旅客账号",
    "description": (
        "首页必须提供 `Register` 链接；注册页提供 `用户名`、`登录密码` 输入框，"
        "提交按钮 accessible name 为 `下一步`；有效提交后导航回 `/`。"
    ),
    "scenarios": [
        {"name": "happy path", "given": "用户在注册页", "when": "填写有效资料", "then": "创建会话并回首页"},
    ],
}

INTERFACES = [
    {
        "interface_id": "REQ-1-API-Auth",
        "type": "API",
        "specification": "register returns 200 + Set-Cookie; /me restores current user from cookie.",
        "responsibility": "Auth boundary",
        "file_path": "backend/src/routes/auth.js",
        "first_line": "router.post('/register'",
    },
]


def test_extract_hooks_from_playwright_spec() -> None:
    hooks = extract_test_hooks("backend/test-e2e/register.e2e.spec.js", E2E_SPEC)
    kinds = {(h["kind"], h["value"]) for h in hooks}
    assert ("label", "用户名") in kinds
    assert ("label", "登录密码") in kinds
    assert ("placeholder", "再次输入密码") in kinds
    assert ("role", "Register") in kinds
    assert ("testid", "submit-btn") in kinds
    assert ("url", "/register") in kinds
    assert ("api", "/api/auth/me") in kinds
    # Regex role names are split into their alternatives; both count as hooks.
    assert ("role", "下一步") in kinds
    assert ("role", "提交") in kinds
    # Every hook records its source file.
    assert all(h["file_path"] == "backend/test-e2e/register.e2e.spec.js" for h in hooks)


def test_extract_hooks_from_integration_spec() -> None:
    hooks = extract_test_hooks("backend/tests/authApi.test.js", INTEGRATION_SPEC)
    api_paths = sorted(h["value"] for h in hooks if h["kind"] == "api")
    assert api_paths == ["/api/auth/logout", "/api/auth/register"]


def test_extract_hooks_skips_degenerate_values() -> None:
    """A bare "/" or symbols-only regex fragment is not an implementable hook."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "await page.goto('/');\nawait expect(page).toHaveURL(/\\/$/);\n",
    )
    assert hooks == []


def test_universe_contains_requirement_and_interface_text() -> None:
    universe = build_satisfiability_universe(REQ_DATA, INTERFACES)
    assert "用户名" in universe
    assert "Register" in universe
    assert "下一步" in universe
    assert "/me restores current user" in universe
    assert "happy path" in universe  # scenario names participate too


def test_classify_splits_grounded_and_test_contract() -> None:
    hooks = extract_test_hooks("backend/test-e2e/register.e2e.spec.js", E2E_SPEC)
    universe = build_satisfiability_universe(REQ_DATA, INTERFACES)
    result = classify_test_hooks(hooks, universe)

    grounded = {(h["kind"], h["value"]) for h in result["grounded"]}
    contract = {(h["kind"], h["value"]) for h in result["test_contract"]}
    # Spelled out in the requirement text.
    assert ("label", "用户名") in grounded
    assert ("role", "Register") in grounded
    assert ("url", "/register") in grounded
    assert ("role", "下一步") in grounded
    # Not spelled out anywhere: test-defined contract hooks.
    assert ("label", "Sign out") not in grounded
    assert ("placeholder", "再次输入密码") in contract
    assert ("testid", "submit-btn") in contract
    assert ("label", "登录密码") in grounded  # in requirement description
    # Grounded hooks carry the matched-source marker.
    assert all(h["source"] == "requirement-or-interface" for h in result["grounded"])
    assert all(h["source"] == "test_contract" for h in result["test_contract"])


def test_classify_is_case_and_whitespace_insensitive() -> None:
    hooks = [{"kind": "label", "value": "  用户名  ", "file_path": "a"}]
    result = classify_test_hooks(hooks, "registration form has 用户名 field")
    assert result["grounded"] and not result["test_contract"]


def test_collect_manifest_hooks_reads_only_e2e_and_integration(tmp_path: Path) -> None:
    e2e = tmp_path / "backend" / "test-e2e" / "spec.js"
    e2e.parent.mkdir(parents=True)
    e2e.write_text(E2E_SPEC, encoding="utf-8")
    unit = tmp_path / "tests" / "unit" / "calc.test.js"
    unit.parent.mkdir(parents=True)
    unit.write_text(UNIT_SPEC, encoding="utf-8")
    missing = tmp_path / "tests" / "integration" / "gone.test.js"

    tests = [
        {"type": "E2E", "file_path": "backend/test-e2e/spec.js"},
        {"type": "Unit", "file_path": "tests/unit/calc.test.js"},
        {"type": "Integration", "file_path": "tests/integration/gone.test.js"},
        # duplicate file path collapses
        {"type": "E2E", "file_path": "backend/test-e2e/spec.js"},
    ]
    hooks = collect_manifest_hooks(str(tmp_path), tests)
    files = {h["file_path"] for h in hooks}
    assert files == {"backend/test-e2e/spec.js"}
    assert ("label", "用户名") in {(h["kind"], h["value"]) for h in hooks}
    # Missing files are skipped silently (unwritable/moved manifests must not
    # break the DESIGN phase).


def test_format_test_contract_context_renders_hook_block() -> None:
    text = format_test_contract_context(
        [
            {"kind": "label", "value": "再次输入密码", "file_path": "e2e/spec.js"},
            {"kind": "testid", "value": "submit-btn", "file_path": "e2e/spec.js"},
        ]
    )
    assert "再次输入密码" in text
    assert "submit-btn" in text
    assert "e2e/spec.js" in text
    assert "implementation must align to" in text


def test_format_test_contract_context_empty_returns_empty() -> None:
    assert format_test_contract_context([]) == ""


def test_extract_hooks_name_not_first_role_option() -> None:
    """``{ exact: true, name: ... }`` — name after other options still matches."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "page.getByRole('link', { exact: true, name: 'Register' }).click();",
    )
    assert ("role", "Register") in {(h["kind"], h["value"]) for h in hooks}


def test_extract_hooks_nested_option_object() -> None:
    """A nested option object before ``name`` must not derail the match."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "page.getByRole('row', { name: 'row1' }).filter({ hasText: 'detail' });",
    )
    assert ("role", "row1") in {(h["kind"], h["value"]) for h in hooks}


def test_extract_hooks_goto_new_url_form() -> None:
    """``goto(new URL('/login', base))`` — the path is still a URL hook."""

    hooks = extract_test_hooks("x.spec.js", "await page.goto(new URL('/login', base));")
    assert ("url", "/login") in {(h["kind"], h["value"]) for h in hooks}


def test_normalize_strips_origin_to_path() -> None:
    """Full origins reduce to their path; a bare origin reduces to nothing.

    ``http://localhost:3301/register`` -> ``register`` is the surface a
    requirement could name; ``http://localhost:3301`` alone carries none.
    """

    from agents.tools.test_contract_check import _normalize_for_match

    assert _normalize_for_match("http://localhost:3301/register") == "register"
    assert _normalize_for_match("http://localhost:3301") == ""
    assert _normalize_for_match("https://example.com/api/auth") == "api/auth"
    assert _normalize_for_match(r"\/register$") == "/register"

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

import pytest

from agents.tools.test_contract_check import (
    build_satisfiability_universe,
    classify_test_hooks,
    collect_manifest_hooks,
    extract_http_status_assertions,
    extract_test_hooks,
    format_test_contract_context,
    validate_http_status_contracts,
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


def test_extract_http_status_assertions_keeps_request_context() -> None:
    content = """
const response = await page.request.post('/api/notes');
expect(response.status()).toBe(201);
"""

    assertions = extract_http_status_assertions("e2e/notes.spec.js", content)

    assert assertions == [
        {
            "file_path": "e2e/notes.spec.js",
            "line": 3,
            "path": "/api/notes",
            "method": "POST",
            "assertion": "expect(response.status()).toBe(201)",
            "expected_status_codes": [201],
            "matcher": "toBe",
        }
    ]


def test_extract_http_status_assertions_supports_supertest_request_chain() -> None:
    content = """
const response = await request(app).post('/api/notes');
expect(response.status).toBe(201);
"""

    assertions = extract_http_status_assertions("tests/notes.test.js", content)

    assert assertions[0]["path"] == "/api/notes"
    assert assertions[0]["method"] == "POST"
    assert assertions[0]["expected_status_codes"] == [201]


@pytest.mark.parametrize(
    ("content", "expected_codes"),
    [
        (
            "const response = await request(app).post('/api/notes').expect(201);",
            [201],
        ),
        (
            "expect(response).toHaveProperty('status', 201);",
            [201],
        ),
        (
            "expect([200, 201]).toContain(response.status());",
            [200, 201],
        ),
    ],
)
def test_extract_http_status_assertions_supports_status_assertion_shapes(
    content: str, expected_codes: list[int]
) -> None:
    assertions = extract_http_status_assertions(
        "tests/notes.test.js",
        "const response = await request(app).post('/api/notes');\n" + content,
    )

    assert assertions
    assert assertions[-1]["expected_status_codes"] == expected_codes


def test_extract_http_status_assertions_ignores_supertest_header_expectation() -> None:
    content = "const response = await request(app).get('/api/notes').expect('Content-Type', /json/);"

    assert extract_http_status_assertions("tests/notes.test.js", content) == []


@pytest.mark.parametrize("status_code", [200, 201, 206])
def test_validate_http_status_contract_accepts_explicit_interface_status(
    tmp_project_dir: Path, status_code: int
) -> None:
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        f"expect(response.status()).toBe({status_code});\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns HTTP status code "
                f"{status_code}.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_uses_interface_output_without_defaulting_to_200(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": {"status_code": 201},
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


@pytest.mark.parametrize("response_field", ["outputs", "responses"])
def test_validate_http_status_contract_accepts_arc_output2_auth_shapes(
    tmp_project_dir: Path, response_field: str,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "auth.routes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.post('/register', (req, res) => {\n"
        "  // 201 -> { user }\n"
        "  // 400 -> validation error\n"
        "  // 409 -> duplicate email\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n"
        "router.get('/me', (req, res) => {\n"
        "  // 200 -> { user }\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n"
        "router.post('/logout', (req, res) => {\n"
        "  // 200 -> { ok: true }\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "auth.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const register = await request.post('/api/auth/register');\n"
        "expect(register.status).toBe(201);\n"
        "expect(register.status).toBe(400);\n"
        "expect(register.status).toBe(409);\n"
        "const me = await request.get('/api/auth/me');\n"
        "expect(me.status).toBe(200);\n"
        "const logout = await request.post('/api/auth/logout');\n"
        "expect(logout.status).toBe(200);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "IF-AUTH",
        "type": "API",
        "specification": "POST /api/auth/register, GET /api/auth/me, POST /api/auth/logout. "
        "Status codes fixed: register 201 success, 400 field validation, "
        "409 duplicates; me/logout always 200.",
        response_field: {
            "register": "201 { user }; 400 { errors }; 409 { error }",
            "me": "200 { user }",
            "logout": "200 { ok: true }",
        },
        "file_path": "backend/src/routes/auth.routes.js",
    }
    manifest = [{"type": "Integration", "file_path": "integration/auth.test.js"}]

    assert validate_http_status_contracts(
        tmp_project_dir, {"description": "Authentication."}, [interface], manifest
    ) == []

    for field in (response_field, "specification"):
        partial = {**interface}
        partial.pop(field)
        partial["file_path"] = "backend/src/routes/missing.js"
        assert validate_http_status_contracts(
            tmp_project_dir, {"description": "Authentication."}, [partial], manifest
        ) == []


def test_validate_http_status_contract_route_comments_are_scoped_and_placeholder_is_not_a_contract(
    tmp_project_dir: Path,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        "  // 201 -> created\n"
        "  return res.status(500).json({\n"
        "    code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)'\n"
        "  });\n"
        "});\n"
        "router.get('/users', (req, res) => {\n"
        "  // 200 -> users\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(201);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "IF-NOTES",
        "type": "API",
        "specification": "POST /api/notes creates a note.",
        "file_path": "backend/src/routes/notes.js",
    }
    manifest = [{"type": "Integration", "file_path": "integration/notes.test.js"}]

    assert validate_http_status_contracts(tmp_project_dir, {"description": "Create a note."}, [interface], manifest) == []

    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(500);\n",
        encoding="utf-8",
    )
    diagnostics = validate_http_status_contracts(
        tmp_project_dir, {"description": "Create a note."}, [interface], manifest
    )
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [201]


@pytest.mark.parametrize(
    "placeholder",
    ["code: 'NOT_IMPLEMENTED'", "message: 'TODO(TDD)'", "message: 'TODO(TDD); work pending'"],
)
def test_validate_http_status_contract_placeholder_only_requires_status_declaration(
    tmp_project_dir: Path, placeholder: str,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        f"  return res.status(500).json({{ {placeholder} }});\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(500);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [{"interface_id": "IF-NOTES", "type": "API", "file_path": "backend/src/routes/notes.js"}],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"


def test_validate_http_status_contract_preserves_real_status_next_to_placeholder(tmp_project_dir: Path) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        "  if (req.invalid) return res.status(400).json({ error: 'invalid' });\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(400);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [{"interface_id": "IF-NOTES", "type": "API", "file_path": "backend/src/routes/notes.js"}],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_top_level_status_field(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "status_code": 201,
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_nested_output_code_field(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": [{"code": 201, "body": "created"}],
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_accepts_declared_response_status_set(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBeOneOf([200, 201]);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": {"status_codes": [200, 201]},
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_uses_requirement_status_when_interface_is_silent(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(202);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "POST /api/notes returns HTTP status code 202."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_explicit_route_status(tmp_project_dir: Path) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.post('/notes', (req, res) => res.status(206).json({}));\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        "expect(response.status()).toBe(206);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_does_not_mix_statuses_from_sibling_routes(
    tmp_project_dir: Path,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.get('/users', (req, res) => res.status(200).json({}));\n"
        "router.post('/notes', (req, res) => res.status(201).json({}));\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request(app).post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [201]


def test_validate_http_status_contract_rejects_requirement_interface_conflict(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "POST /api/notes returns 201."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 200.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert "201" in diagnostics[0]["message"]
    assert "200" in diagnostics[0]["message"]


def test_validate_http_status_contract_rejects_conflicting_assertion_with_diagnostic(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 201.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["code"] == "status_code_conflict"
    assert diagnostic["file_path"] == "e2e/notes.spec.js"
    assert diagnostic["assertion"] == "expect(response.status()).toBe(200)"
    assert diagnostic["expected_status_codes"] == [200]
    assert diagnostic["contract_status_codes"] == [201]
    assert diagnostic["interface_id"] == "IF-NOTES"
    assert "201" in diagnostic["message"]


def test_validate_http_status_contract_reports_needs_info_without_status_source(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "needs-info" in diagnostics[0]["message"]
    assert "not enough HTTP status" in diagnostics[0]["message"]


def test_validate_http_status_contract_rejects_unbounded_2xx_matcher(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBeGreaterThanOrEqual(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 201.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "specific status code" in diagnostics[0]["message"]
